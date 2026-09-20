"""Transformações puras usadas pela interface e pelos testes."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pandas as pd


STATUS_LABEL = {
    'A_REVISAR': 'A revisar', 'VALIDADA': 'Validada', 'DESCARTADA': 'Descartada',
    'PRECISA_INFORMACAO': 'Precisa de informação', 'SUCESSO': 'Sucesso',
    'PARCIAL': 'Parcial', 'FALHA': 'Falha', 'PENDENTE': 'Pendente',
    'ENVIADA': 'Enviada', 'RESULTADO_INCERTO': 'Resultado incerto',
}


def moeda(value) -> str:
    if value in (None, '') or pd.isna(value):
        return 'Não informado'
    try:
        number = Decimal(str(value))
        return 'R$ ' + f'{number:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    except (InvalidOperation, ValueError):
        return 'Não informado'


def data_br(value, com_hora=False) -> str:
    if value in (None, '') or pd.isna(value):
        return 'Não informado'
    if not com_hora and isinstance(value, str) and len(value) == 10:
        instant = pd.to_datetime(value, errors='coerce')
        return instant.strftime('%d/%m/%Y') if not pd.isna(instant) else 'Não informado'
    instant = pd.to_datetime(value, utc=True, errors='coerce')
    if pd.isna(instant):
        return 'Não informado'
    instant = instant.tz_convert('America/Sao_Paulo')
    return instant.strftime('%d/%m/%Y %H:%M' if com_hora else '%d/%m/%Y')


def filtrar(rows: list[dict], busca='', status=None, fontes=None, municipios=None,
            somente_alteradas=False) -> list[dict]:
    search = busca.casefold().strip()
    statuses, source_set, cities = set(status or []), set(fontes or []), set(municipios or [])
    result = []
    for row in rows:
        haystack = ' '.join(str(row.get(k) or '') for k in ('objeto', 'orgao', 'municipio', 'id_externo')).casefold()
        if search and search not in haystack:
            continue
        if statuses and row.get('status_revisao') not in statuses:
            continue
        if source_set and row.get('fonte') not in source_set:
            continue
        if cities and row.get('municipio') not in cities:
            continue
        if somente_alteradas and not row.get('alteracao_pendente'):
            continue
        result.append(row)
    return result


def metricas(rows: list[dict]) -> dict:
    total = len(rows)
    statuses = {key: sum(r.get('status_revisao') == key for r in rows)
                for key in ('A_REVISAR', 'VALIDADA', 'DESCARTADA', 'PRECISA_INFORMACAO')}
    value = sum(Decimal(str(r['valor_estimado'])) for r in rows if r.get('valor_estimado') not in (None, ''))
    return {'total': total, **statuses,
            'alteradas': sum(bool(r.get('alteracao_pendente')) for r in rows), 'valor_total': value}
