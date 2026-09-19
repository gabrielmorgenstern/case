"""Validação e planejamento da carga; nenhuma chamada de rede neste módulo."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

FONTES = ('PNCP', 'TCE_RJ')
META = {'coletado_em', 'motivo_revisao'}
TEXTOS = ('status_comercial classificacao_glp municipio orgao unidade_administrativa objeto '
          'modalidade modo_disputa situacao numero_compra processo cnpj_orgao link_pncp '
          'link_sistema_origem link_processo_eletronico referencia_publicacao_oficial '
          'codigo_ibge_municipio uf esfera poder sequencial_compra informacao_complementar').split()


def agora():
    return datetime.now(timezone.utc)


def instante(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Horário sem fuso informado')
    return dt.astimezone(timezone.utc)


def vazio(value):
    return value is None or (isinstance(value, str) and value.strip().lower() in ('', 'null', 'none', 'nan', 'nat'))


def conteudo(record):
    return {k: v for k, v in record.items() if k not in META}


def erro(categoria, mensagem, **extra):
    return {'categoria': categoria, 'mensagem': mensagem, 'horario': agora().isoformat(), **extra}


@dataclass
class ResultadoFonte:
    fonte: str
    registros: list[dict] = field(default_factory=list)
    status: str = 'DESCONHECIDO'
    erros: list[dict] = field(default_factory=list)
    recebidos: int | None = None
    requisicoes: int | None = None
    iniciada_em: datetime | None = None
    finalizada_em: datetime | None = None


@dataclass
class Execucao:
    chave: str
    origem: str
    parametros: dict
    fontes: list[ResultadoFonte]
    ordem: datetime


def normalizar_registro(record, fonte):
    if not isinstance(record, dict) or record.get('fonte') != fonte:
        raise ValueError('Registro sem fonte correspondente')
    if not isinstance(record.get('id_oportunidade'), str) or not record['id_oportunidade'].strip():
        raise ValueError('Identificador externo ausente ou inválido')
    json.dumps(record, allow_nan=False)
    collected = instante(record.get('coletado_em'))
    warnings = []
    values = {k: None if vazio(record.get(k)) else str(record[k]) for k in TEXTOS}

    def convert(key, transform, dest=None):
        dest = dest or key
        values[dest] = None
        if not vazio(record.get(key)):
            try:
                values[dest] = transform(record[key])
            except (ValueError, TypeError, InvalidOperation, OverflowError):
                warnings.append({'campo': key, 'motivo': 'Formato inválido ou informação insuficiente; original preservado'})

    def integer(v):
        d = Decimal(str(v))
        if not d.is_finite() or d != d.to_integral_value() or not -(2**31) <= d < 2**31:
            raise ValueError('Inteiro inválido')
        return int(d)

    def money(v):
        d = Decimal(str(v))
        if not d.is_finite() or abs(d) >= Decimal('1e18') or d != d.quantize(Decimal('.01')):
            raise ValueError('Valor incompatível com numeric(20,2)')
        return d

    def boolean(v):
        if isinstance(v, bool):
            return v
        if str(v).lower() in ('true', 'false'):
            return str(v).lower() == 'true'
        raise ValueError('Booleano inválido')

    for k in ('score_glp', 'ano_compra'):
        convert(k, integer)
    convert('valor_estimado', money)
    convert('srp', boolean)
    for k in ('termos_fortes', 'termos_suporte', 'termos_exclusao'):
        convert(k, lambda v: [s.strip() for s in v.split(',') if s.strip()] if isinstance(v, str) else list(v))
    for k in ('abertura_propostas', 'encerramento_propostas'):
        convert(k, instante)
    for k in ('data_publicacao_oficial', 'data_homologacao'):
        convert(k, lambda v: date.fromisoformat(str(v)))
    values['data_publicacao_pncp'] = values['data_publicacao_edital'] = None
    if fonte == 'PNCP':
        convert('data_publicacao_pncp', instante)
    else:
        convert('data_publicacao_pncp', lambda v: date.fromisoformat(str(v)), 'data_publicacao_edital')
    if values['data_homologacao'] and record.get('situacao') == 'SEM_HOMOLOGACAO_REGISTRADA':
        warnings.append({'campo': 'situacao', 'motivo': 'Status do coletor contradiz a data de homologação preenchida'})
    if record.get('status_comercial') == 'ABERTA_CONFIRMADA' and values['encerramento_propostas'] is None:
        warnings.append({'campo': 'encerramento_propostas', 'motivo': 'Prazo ausente ou sem fuso; abertura não confirmada pela integração'})
    return {'captura': record, 'coletado_em': collected, 'inconsistencias': warnings, **values}


def planejar(registros, fonte, fonte_id, consulta_fonte_id, existentes):
    """Uma decisão por ID. Planeja três lotes sem acessar o banco."""
    licitacoes, versoes, itens, rejeitados = [], [], [], []
    contadores = {'novos': 0, 'alterados': 0, 'inalterados': 0, 'rejeitados': 0}
    vistos = {}
    for record in registros:
        key = record.get('id_oportunidade') if isinstance(record, dict) else None
        try:
            typed = normalizar_registro(record, fonte)
            if key in vistos:
                raise ValueError('Identificador duplicado na mesma captura; revisar a entrada')
            vistos[key] = True
            known = existentes.get(key)
            if known:
                last = known.get('ultima_observacao') or known['coletado_em']
                if typed['coletado_em'] < last:
                    raise ValueError('Captura histórica anterior ao estado já registrado')
                changed = conteudo(record) != conteudo(known['captura'])
                if changed and typed['coletado_em'] <= last:
                    raise ValueError('Conteúdo diferente sem horário posterior')
                lid = known['licitacao_id']
                vid = uuid4() if changed else known['versao_id']
                result = 'ALTERADA' if changed else 'INALTERADA'
            else:
                lid, vid = uuid4(), uuid4()
                result = 'NOVA'
                licitacoes.append({'id': lid, 'fonte_id': fonte_id, 'id_externo': key,
                    'primeira_consulta_fonte_id': consulta_fonte_id, 'captura_original': record,
                    'primeiro_coletado_em': typed['coletado_em']})
            if result != 'INALTERADA':
                versoes.append({'id': vid, 'licitacao_id': lid, 'fonte_id': fonte_id,
                    'consulta_fonte_id': consulta_fonte_id, **typed})
            itens.append({'consulta_fonte_id': consulta_fonte_id, 'licitacao_id': lid, 'fonte_id': fonte_id,
                'versao_id': vid, 'resultado': result, 'coletado_em': typed['coletado_em'],
                'motivo_revisao_coleta': record.get('motivo_revisao')})
            contadores[{'NOVA': 'novos', 'ALTERADA': 'alterados', 'INALTERADA': 'inalterados'}[result]] += 1
        except (ValueError, TypeError) as exc:
            contadores['rejeitados'] += 1
            rejeitados.append({'id_oportunidade': key, 'motivo': str(exc), 'registro': record})
    return licitacoes, versoes, itens, contadores, rejeitados


def carregar_arquivo(path: Path):
    records = [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if not records:
        raise ValueError('Arquivo vazio não informa o instante de captura: ' + path.name)
    times = {instante(r.get('coletado_em')) for r in records}
    if len(times) != 1 or any(r.get('fonte') not in FONTES for r in records):
        raise ValueError('Arquivo deve conter uma captura e fontes conhecidas: ' + path.name)
    canonical = json.dumps(sorted(records, key=lambda r: (r['fonte'], r.get('id_oportunidade', ''))),
                           ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    sources = []
    for fonte in FONTES:
        rows = [r for r in records if r['fonte'] == fonte]
        result = ResultadoFonte(fonte=fonte, registros=rows)
        # O arquivo comprova a indicação de falha, mas não seu código HTTP nem causa técnica.
        if fonte == 'PNCP' and not rows and any('PNCP indisponível:' in str(r.get('motivo_revisao', '')) for r in records):
            result.status = 'FALHA'
            result.erros = [erro('INDETERMINADO', 'Arquivo histórico informa indisponibilidade do PNCP; sem telemetria da execução original', etapa='IMPORTACAO_HISTORICA')]
        sources.append(result)
    return Execucao('arquivo:' + hashlib.sha256(canonical.encode()).hexdigest(), 'IMPORTACAO_HISTORICA',
        {'arquivo': path.name, 'capturado_em': next(iter(times)).isoformat()}, sources, next(iter(times)))
