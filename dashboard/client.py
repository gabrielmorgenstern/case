"""Cliente mínimo para Supabase Auth e PostgREST, sem credenciais no código."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests


class ServicoIndisponivel(RuntimeError):
    """Erro seguro para exibição, sem resposta interna ou credenciais."""


class AcessoNegado(RuntimeError):
    pass


@dataclass
class SessaoUsuario:
    access_token: str
    refresh_token: str
    expira_em: float
    usuario_id: str
    email: str


class Supabase:
    def __init__(self, url: str, anon_key: str, timeout: int = 15):
        self.url = url.rstrip('/')
        self.anon_key = anon_key
        self.timeout = timeout
        self.http = requests.Session()

    def _call(self, method: str, path: str, *, token: str | None = None,
              schema: str | None = None, **kwargs) -> requests.Response:
        headers = {'apikey': self.anon_key}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        if schema:
            headers['Accept-Profile'] = schema
            headers['Content-Profile'] = schema
        headers.update(kwargs.pop('headers', {}))
        try:
            response = self.http.request(method, self.url + path, headers=headers,
                                         timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ServicoIndisponivel('Não foi possível acessar o serviço de dados.') from exc
        return response

    def entrar(self, email: str, senha: str) -> SessaoUsuario:
        response = self._call('POST', '/auth/v1/token?grant_type=password',
                              json={'email': email, 'password': senha})
        if response.status_code in (400, 401):
            raise AcessoNegado('E-mail ou senha inválidos.')
        if not response.ok:
            raise ServicoIndisponivel('A autenticação está temporariamente indisponível.')
        data = response.json()
        return SessaoUsuario(
            access_token=data['access_token'], refresh_token=data['refresh_token'],
            expira_em=time.time() + int(data.get('expires_in', 3600)),
            usuario_id=data['user']['id'], email=data['user'].get('email', email),
        )

    def renovar(self, sessao: SessaoUsuario) -> SessaoUsuario:
        response = self._call('POST', '/auth/v1/token?grant_type=refresh_token',
                              json={'refresh_token': sessao.refresh_token})
        if not response.ok:
            raise AcessoNegado('Sua sessão expirou. Entre novamente.')
        data = response.json()
        return SessaoUsuario(
            access_token=data['access_token'], refresh_token=data['refresh_token'],
            expira_em=time.time() + int(data.get('expires_in', 3600)),
            usuario_id=data['user']['id'], email=data['user'].get('email', sessao.email),
        )

    def sair(self, sessao: SessaoUsuario) -> None:
        self._call('POST', '/auth/v1/logout', token=sessao.access_token)

    def _json(self, response: requests.Response) -> Any:
        if response.status_code in (401, 403):
            raise AcessoNegado('Acesso não autorizado ou sessão expirada.')
        if not response.ok:
            raise ServicoIndisponivel('Não foi possível concluir a operação no banco.')
        return response.json() if response.content else None

    def selecionar(self, sessao: SessaoUsuario, recurso: str, *, params: dict | None = None,
                   limite: int = 1000) -> list[dict]:
        params = dict(params or {})
        params.setdefault('limit', str(limite))
        response = self._call('GET', f'/rest/v1/{quote(recurso, safe="")}', token=sessao.access_token,
                              schema='glp', params=params)
        data = self._json(response)
        return data if isinstance(data, list) else []

    def rpc(self, sessao: SessaoUsuario, funcao: str, argumentos: dict) -> Any:
        response = self._call('POST', f'/rest/v1/rpc/{quote(funcao, safe="")}',
                              token=sessao.access_token, schema='glp', json=argumentos)
        return self._json(response)

    def usuario_ativo(self, sessao: SessaoUsuario) -> dict | None:
        rows = self.selecionar(sessao, 'usuarios', params={
            'select': 'id,nome_exibicao,ativo', 'id': f'eq.{sessao.usuario_id}', 'ativo': 'eq.true'}, limite=1)
        return rows[0] if rows else None
