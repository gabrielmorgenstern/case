"""Envio ao Telegram com resultado explícito para a fila transacional."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import requests


@dataclass(frozen=True)
class ResultadoEnvio:
    status: str
    id_mensagem: str | None = None
    erro: str | None = None


def _valor(value) -> str:
    if value in (None, ''):
        return 'Não informado'
    try:
        number = Decimal(str(value))
        return 'R$ ' + f'{number:,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    except InvalidOperation:
        return 'Não informado'


def montar_mensagem(item: dict, revisor: str) -> str:
    return '\n'.join([
        '✅ LICITAÇÃO GLP VALIDADA', '',
        f"🏛️ Órgão:  {item.get('orgao') or 'Não informado'}",
        f"📍 Município:  {item.get('municipio') or 'Não informado'}", '',
        '📋 Objeto:',
        str(item.get('objeto') or 'Não informado'), '',
        f"💰 Valor estimado:  {_valor(item.get('valor_estimado'))}",
        f"⏰ Prazo:  {item.get('encerramento_propostas') or 'Não informado'}",
        f"🔎 Fonte:  {item.get('fonte') or 'Não informada'}", '',
        f"👤 Validada por:  {revisor or 'Usuário autorizado'}",
    ])


def enviar(token: str, chat_id: str, mensagem: str, timeout: int = 30) -> ResultadoEnvio:
    if not token or not chat_id:
        return ResultadoEnvio('PENDENTE', erro='Telegram ainda não configurado.')
    try:
        response = requests.post(f'https://api.telegram.org/bot{token}/sendMessage', json={
            'chat_id': chat_id, 'text': mensagem, 'disable_web_page_preview': True}, timeout=timeout)
    except (requests.Timeout, requests.ConnectionError):
        return ResultadoEnvio('RESULTADO_INCERTO', erro='Não foi possível confirmar o envio.')
    except requests.RequestException:
        return ResultadoEnvio('FALHA', erro='Falha de comunicação com o Telegram.')
    if not response.ok:
        return ResultadoEnvio('FALHA', erro=f'Telegram recusou a mensagem (HTTP {response.status_code}).')
    try:
        data = response.json()
        message_id = str(data['result']['message_id']) if data.get('ok') else None
    except (ValueError, KeyError, TypeError):
        return ResultadoEnvio('RESULTADO_INCERTO', erro='Resposta inesperada do Telegram.')
    return ResultadoEnvio('ENVIADA', id_mensagem=message_id) if message_id else ResultadoEnvio(
        'RESULTADO_INCERTO', erro='O Telegram não confirmou o identificador da mensagem.')
