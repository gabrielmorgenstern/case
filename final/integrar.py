"""CLI da integração. Histórico local ou coleta pública; sem dashboard/Telegram."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from integracao_dados import carregar_arquivo, agora, planejar

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='modo', required=True)
    hist = sub.add_parser('importar', help='Importar JSONL já existentes sem consultar APIs')
    hist.add_argument('arquivos', nargs='+', type=Path)
    hist.add_argument('--simular', action='store_true', help='Conferir arquivos sem conexão com o banco')
    live = sub.add_parser('coletar', help='Reutilizar teste.py e gravar resultados no banco')
    live.add_argument('--dias-pncp', type=int, default=365)
    live.add_argument('--ano-tce', type=int, default=agora().year)
    live.add_argument('--municipio-tce')
    live.add_argument('--max-chamadas-fonte', type=int, default=80)
    live.add_argument('--segundos-fonte', type=int, default=300)
    live.add_argument('--intervalo-requisicoes', type=float, default=1.0)
    live.add_argument('--com-detalhes-pncp', action='store_true')
    live.add_argument('--incluir-revisar', action='store_true')
    live.add_argument('--sem-banco', action='store_true', help='Validar as APIs e gerar um relatório local, sem tocar no banco')
    live.add_argument('--chave-execucao', help='Por padrão, uma execução por hora UTC')
    args = parser.parse_args()
    if args.modo == 'coletar' and (args.dias_pncp < 0 or args.max_chamadas_fonte < 1 or
                                  args.segundos_fonte < 1 or args.intervalo_requisicoes < 0):
        parser.error('Limites e intervalos inválidos')
    reports = []
    db = None
    try:
        if args.modo == 'importar':
            # Ler e validar arquivos antes de abrir conexão.
            jobs = sorted((carregar_arquivo(p) for p in args.arquivos), key=lambda e: e.ordem)
            if args.simular:
                known = {f: {} for f in ('PNCP', 'TCE_RJ')}
                seen = set()
                for job in jobs:
                    if job.chave in seen:
                        continue
                    seen.add(job.chave)
                    totals = {}
                    for source in job.fontes:
                        _, versions, _, counts, rejects = planejar(source.registros, source.fonte,
                            'simulado', 'simulado', known[source.fonte])
                        for version in versions:
                            r = version['captura']
                            known[source.fonte][r['id_oportunidade']] = {
                                'licitacao_id': version['licitacao_id'], 'versao_id': version['id'],
                                'captura': r, 'coletado_em': version['coletado_em']}
                        totals[source.fonte] = {**counts, 'rejeicoes': rejects}
                    reports.append({'arquivo': job.parametros['arquivo'], 'fontes': totals})
                print(json.dumps({'modo': 'SIMULACAO_LOCAL', 'execucoes': reports,
                    'licitacoes_distintas': sum(len(v) for v in known.values()), 'chamadas_banco': 0,
                    'chamadas_apis': 0}, ensure_ascii=True, default=str))
                return 0
        else:
            from coleta_integrada import preparar_execucao, coletar_fonte
            jobs = [preparar_execucao(args)]
        if not getattr(args, 'sem_banco', False):
            from integracao_banco import Banco
            db = Banco()
        for job in jobs:
            cid, states = db.iniciar(job) if db else (None, {})
            results = {}
            for source in job.fontes:
                if args.modo == 'coletar':
                    if db and not db.pendente(states[source.fonte]):
                        results[source.fonte] = {**states[source.fonte], 'ignorada': True}
                        continue
                    source = coletar_fonte(source.fonte, args)
                    if db:
                        # Preserva os dados antes da tentativa de gravação no banco.
                        checkpoints = ROOT / 'dados' / 'coletas'
                        checkpoints.mkdir(parents=True, exist_ok=True)
                        checkpoint = checkpoints / (agora().strftime('%Y%m%dT%H%M%S%fZ') + '_' + source.fonte + '.jsonl')
                        with checkpoint.open('x', encoding='utf-8') as file:
                            for record in source.registros:
                                file.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
                if db:
                    results[source.fonte] = db.salvar_fonte(source, states[source.fonte])
                else:
                    results[source.fonte] = {'status': source.status, 'selecionados': len(source.registros),
                        'recebidos': source.recebidos, 'requisicoes': source.requisicoes, 'erros': source.erros,
                        'registros': source.registros}
            status = db.finalizar(cid, results, job.origem == 'IMPORTACAO_HISTORICA') if db else 'SEM_BANCO'
            reports.append({'chave': job.chave, 'consulta_id': cid, 'status': status, 'fontes': results})
        output = {'executado_em': agora().isoformat(), 'comandos_sql_cliente': db.comandos if db else 0,
                  'execucoes': reports}
        folder = ROOT / 'relatorios'
        folder.mkdir(exist_ok=True)
        path = folder / ('integracao_' + agora().strftime('%Y%m%dT%H%M%S%fZ') + '.json')
        path.write_text(json.dumps(output, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        summary = {'relatorio': str(path), 'comandos_sql_cliente': output['comandos_sql_cliente'],
            'execucoes': [{**r, 'fontes': {f: {k: v for k, v in s.items() if k not in ('registros', 'rejeicoes')}
                                         for f, s in r['fontes'].items()}} for r in reports]}
        print(json.dumps(summary, ensure_ascii=True, default=str))
        return 2 if any(s['status'] in ('FALHA', 'PARCIAL') for r in reports for s in r['fontes'].values()) else 0
    except Exception as exc:
        # Não imprimir DSN, texto de libpq ou traceback com parâmetros sensíveis.
        print(json.dumps({'resultado': 'ERRO', 'tipo': type(exc).__name__,
                          'sqlstate': getattr(exc, 'sqlstate', None),
                          'restricao': getattr(getattr(exc, 'diag', None), 'constraint_name', None),
                          'mensagem_banco': getattr(getattr(exc, 'diag', None), 'message_primary', None)}, ensure_ascii=True))
        return 1
    finally:
        if db:
            db.close()


if __name__ == '__main__':
    raise SystemExit(main())
