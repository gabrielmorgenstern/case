"""Orquestra o coletor original com orçamento de chamadas e estado por fonte."""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / '.local-deps'))
import requests
import teste as original

from integracao_dados import Execucao, FONTES, ResultadoFonte, agora, erro


class LimiteColeta(RuntimeError):
    pass


class ContratoFonte(RuntimeError):
    pass


class SessaoLimitada:
    def __init__(self, args, base=None, clock=time.monotonic, sleep=time.sleep):
        self.base = base or original.criar_sessao()
        self.clock, self.sleep = clock, sleep
        self.deadline = clock() + args.segundos_fonte
        self.limite = args.max_chamadas_fonte
        self.intervalo = args.intervalo_requisicoes
        self.ultima = None
        self.chamadas = 0
        self.falhas = {}

    def get(self, url, params=None, timeout=None):
        if self.chamadas >= self.limite or self.clock() >= self.deadline:
            raise LimiteColeta('Orçamento de chamadas ou duração atingido')
        if self.ultima is not None:
            self.sleep(max(0, self.intervalo - (self.clock() - self.ultima)))
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise LimiteColeta('Orçamento de duração atingido')
        key = (url, tuple(sorted((params or {}).items())))
        self.chamadas += 1
        self.ultima = self.clock()
        try:
            response = self.base.get(url, params=params, timeout=(min(10, remaining), min(40, remaining)))
        except (requests.Timeout, requests.ConnectionError) as exc:
            self.falhas[key] = erro('COMUNICACAO', 'Falha de comunicação com a fonte; causa não confirmada', tipo=type(exc).__name__)
            raise
        if response.status_code >= 400:
            self.falhas[key] = erro('RESPOSTA_HTTP_FONTE', 'Resposta de erro do endpoint', http_status=response.status_code)
        else:
            self.falhas.pop(key, None)
        if response.status_code == 200 and url in (original.PNCP_PROPOSTA_URL, original.PNCP_PUBLICACAO_URL):
            try:
                payload = response.json()
            except ValueError as exc:
                raise ContratoFonte('PNCP não retornou JSON') from exc
            if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                raise ContratoFonte('Resposta PNCP sem lista data')
        return response

    def close(self):
        self.base.close()


def preparar_execucao(args):
    now = agora()
    key = args.chave_execucao or ('coleta:' + now.strftime('%Y%m%dT%H'))
    return Execucao(key, 'AGENDADA' if key.startswith('coleta:') else 'MANUAL',
        {'dias_pncp': args.dias_pncp, 'ano_tce': args.ano_tce, 'municipio_tce': args.municipio_tce,
         'max_chamadas_fonte': args.max_chamadas_fonte, 'segundos_fonte': args.segundos_fonte,
         'intervalo_requisicoes': args.intervalo_requisicoes, 'detalhes_pncp': args.com_detalhes_pncp,
         'incluir_revisar': args.incluir_revisar}, [ResultadoFonte(f) for f in FONTES], now)


def anotar_falha(result, exc, etapa):
    if isinstance(exc, LimiteColeta):
        category, message = 'LIMITE_LOCAL', 'Coleta interrompida pelo orçamento local; não é erro da API'
    elif isinstance(exc, (ContratoFonte, original.FonteIndisponivelError)):
        category, message = 'INDETERMINADO', 'Não foi possível concluir esta parte da fonte; consultar telemetria HTTP'
        if isinstance(exc, ContratoFonte):
            category, message = 'CONTRATO_DADOS', 'Resposta fora do formato esperado'
    elif isinstance(exc, requests.HTTPError):
        category, message = 'RESPOSTA_HTTP_FONTE', 'Requisição recusada pelo endpoint'
    else:
        category, message = 'PROCESSAMENTO_INTERNO', 'Falha no processamento desta parte da coleta'
    result.erros.append(erro(category, message, etapa=etapa, tipo=type(exc).__name__))


def paginas_pncp(session, endpoint, start, end, extra):
    """Entrega cada página válida imediatamente; falha posterior não descarta as anteriores."""
    page = 1
    while True:
        params = {'dataInicial': start, 'dataFinal': end, 'uf': 'RJ', 'pagina': page,
                  'tamanhoPagina': 50, **extra}
        payload = original.json_da_resposta(original.requisitar(session, endpoint, params, 'PNCP', tentativas=2), 'PNCP')
        rows = payload.get('data') or []
        if not isinstance(rows, list):
            raise ContratoFonte('PNCP sem lista de registros')
        yield [r for r in rows if isinstance(r, dict)]
        total = int(payload.get('totalPaginas') or page)
        if not rows or page >= total:
            break
        page += 1


def coletar_fonte(fonte, args):
    result = ResultadoFonte(fonte, recebidos=0, requisicoes=0, iniciada_em=agora())
    session = SessaoLimitada(args)
    completed = 0
    stamp = result.iniciada_em.isoformat()
    try:
        if fonte == 'PNCP':
            start, end = original.intervalo_datas(args.dias_pncp)
            summaries = []
            stop = False
            for endpoint, param in ((original.PNCP_PROPOSTA_URL, 'modalidade'),
                                    (original.PNCP_PUBLICACAO_URL, 'codigoModalidadeContratacao')):
                failures = 0
                for modality in original.MODALIDADES_PNCP:
                    try:
                        for rows in paginas_pncp(session, endpoint, start, end, {param: modality}):
                            summaries.extend(rows)
                            completed += 1
                        failures = 0
                    except Exception as exc:
                        anotar_falha(result, exc, f'{endpoint.rsplit("/", 1)[-1]}/{modality}')
                        failures += 1
                        if isinstance(exc, LimiteColeta) or failures >= 2:
                            stop = True
                            break
                if summaries or stop:
                    break
            result.recebidos = len(summaries)
            # Uma compra pode aparecer mais de uma vez; evitar também repetir detalhes.
            summaries = list({str(r.get('numeroControlePNCP') or (r.get('orgaoEntidade'), r.get('anoCompra'), r.get('sequencialCompra'))): r
                              for r in summaries}.values())
            for summary in summaries:
                classification = original.classificar_glp(summary.get('objetoCompra'), summary.get('informacaoComplementar'))
                if not original.candidato(classification, args.incluir_revisar):
                    continue
                detail = {}
                if args.com_detalhes_pncp:
                    try:
                        detail = original.obter_detalhe_pncp(session, summary)
                    except Exception as exc:
                        anotar_falha(result, exc, 'detalhe')
                row = original.registro_pncp(summary, detail, stamp)
                if original.prazo_aberto(row.get('encerramento_propostas')):
                    result.registros.append(row)
        else:
            offset, seen = 0, set()
            while True:
                params = {'ano': args.ano_tce, 'inicio': offset, 'limite': 500}
                if args.municipio_tce:
                    params['municipio'] = args.municipio_tce
                payload = original.json_da_resposta(original.requisitar(session, original.TCE_LICITACOES_URL,
                    params, 'TCE-RJ'), 'TCE-RJ')
                if payload and not any(isinstance(payload.get(k), list) for k in
                    ('Licitacoes', 'licitacoes', 'data', 'dados', 'items', 'resultados', 'results')):
                    raise ContratoFonte('TCE sem lista reconhecida')
                rows = original.lista_tce(payload)
                completed += 1
                result.recebidos += len(rows)
                if not rows:
                    break
                fresh = 0
                for item in rows:
                    key = '|'.join(original.normalizar(item.get(c, '')) for c in
                                   ('Ente', 'ProcessoLicitatorio', 'NumeroEdital', 'Ano', 'Objeto'))
                    if key in seen:
                        continue
                    seen.add(key)
                    fresh += 1
                    if original.candidato(original.classificar_glp(item.get('Objeto')), args.incluir_revisar):
                        result.registros.append(original.registro_tce(item, stamp, 'Consulta independente do PNCP.'))
                if len(rows) < 500 or not fresh:
                    break
                offset += len(rows)
    except Exception as exc:
        anotar_falha(result, exc, 'coleta')
    finally:
        result.erros.extend(session.falhas.values())
        result.requisicoes = session.chamadas
        result.finalizada_em = agora()
        session.close()
    result.registros = original.deduplicar(result.registros)
    result.status = ('PARCIAL' if completed else 'FALHA') if result.erros else 'SUCESSO'
    return result
