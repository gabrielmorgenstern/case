"""Persistência em lotes e com uma conexão; não executa DDL nem revisão."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

from integracao_dados import agora, erro, planejar

ROOT = Path(__file__).resolve().parent
JSON_COLS = {'captura', 'captura_original', 'inconsistencias', 'parametros', 'erros'}


def configuracao():
    cfg = {}
    local = ROOT / '.env.coleta.local'
    if local.exists():
        for line in local.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                cfg[key.strip()] = value.strip()
    for key in ('PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'PGSSLMODE'):
        if key in os.environ:
            cfg[key] = os.environ[key]
    missing = [k for k in ('PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD') if not cfg.get(k)]
    if missing:
        raise ValueError('Configuração da coleta incompleta: ' + ', '.join(missing))
    if cfg['PGUSER'].split('.')[0] == 'postgres':
        raise ValueError('Use o login exclusivo de coleta, não o administrador postgres')
    if cfg.get('PGSSLMODE', 'require') not in ('require', 'verify-ca', 'verify-full'):
        raise ValueError('A conexão deve exigir TLS')
    return cfg


class Banco:
    def __init__(self):
        sys.path.insert(0, str(ROOT / '.local-deps'))
        import psycopg
        from psycopg import sql
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        self.sql, self.Jsonb = sql, Jsonb
        cfg = configuracao()
        self.comandos = 0
        self.consultas_finalizadas = set()
        self.conn = psycopg.connect(host=cfg['PGHOST'], port=cfg['PGPORT'], dbname=cfg['PGDATABASE'],
            user=cfg['PGUSER'], password=cfg['PGPASSWORD'], sslmode=cfg.get('PGSSLMODE', 'require'),
            connect_timeout=15, autocommit=True, row_factory=dict_row,
            application_name='case_glp_ingestao', prepare_threshold=None,
            options='-c statement_timeout=30000 -c lock_timeout=5000')
        try:
            self.execute('set role glp_coletor')
            if not self.execute('select pg_try_advisory_lock(739146052) as obtido').fetchone()['obtido']:
                raise RuntimeError('Outra ingestão está em andamento; nenhuma espera ou repetição foi iniciada')
            self.fontes = {r['codigo']: r['id'] for r in self.execute('select id, codigo from glp.fontes').fetchall()}
        except Exception:
            self.conn.close()
            raise

    def close(self):
        # O encerramento libera também o advisory lock de sessão.
        self.conn.close()

    def execute(self, query, params=None):
        self.comandos += 1
        return self.conn.execute(query, params)

    def inserir_lote(self, table, rows):
        if not rows:
            return
        cols = list(rows[0])
        for start in range(0, len(rows), 250):
            chunk = rows[start:start + 250]
            values = [self.Jsonb(r[k]) if k in JSON_COLS else r[k] for r in chunk for k in cols]
            row_sql = self.sql.SQL('(') + self.sql.SQL(',').join(self.sql.Placeholder() for _ in cols) + self.sql.SQL(')')
            query = self.sql.SQL('insert into glp.{} ({}) values {}').format(
                self.sql.Identifier(table), self.sql.SQL(',').join(map(self.sql.Identifier, cols)),
                self.sql.SQL(',').join(row_sql for _ in chunk))
            self.execute(query, values)

    def iniciar(self, execucao):
        existing = self.execute('''select c.id, c.status,
            jsonb_object_agg(f.codigo, jsonb_build_object('id', cf.id, 'status', cf.status,
                'erros', cf.erros, 'novos', cf.novos, 'alterados', cf.alterados,
                'inalterados', cf.inalterados, 'rejeitados', cf.rejeitados)) as fontes
            from glp.consultas c join glp.consultas_fontes cf on cf.consulta_id = c.id
            join glp.fontes f on f.id = cf.fonte_id where c.chave_execucao = %s group by c.id''',
            (execucao.chave,)).fetchone()
        if existing:
            if existing['status'] != 'EM_EXECUCAO':
                self.consultas_finalizadas.add(str(existing['id']))
            return existing['id'], existing['fontes']
        cid = uuid4()
        states = {s.fonte: {'id': uuid4(), 'status': 'EM_EXECUCAO', 'erros': []} for s in execucao.fontes}
        with self.conn.transaction():
            self.inserir_lote('consultas', [{'id': cid, 'chave_execucao': execucao.chave,
                'origem_execucao': execucao.origem, 'parametros': execucao.parametros}])
            self.inserir_lote('consultas_fontes', [{'id': states[s.fonte]['id'], 'consulta_id': cid,
                'fonte_id': self.fontes[s.fonte]} for s in execucao.fontes])
        return cid, states

    @staticmethod
    def pendente(state):
        return state['status'] == 'EM_EXECUCAO' or any(
            e.get('categoria') == 'PERSISTENCIA_INTERNA' for e in state.get('erros', []))

    def salvar_fonte(self, source, state):
        if not self.pendente(state):
            return {**state, 'ignorada': True}
        ids = [r.get('id_oportunidade') for r in source.registros if isinstance(r, dict) and isinstance(r.get('id_oportunidade'), str)]
        try:
            with self.conn.transaction():
                # Uma leitura de candidatos por fonte, não uma consulta por registro.
                known = self.execute('''select l.id as licitacao_id, l.id_externo, v.id as versao_id,
                    v.captura, v.coletado_em,
                    (select max(i.coletado_em) from glp.consulta_itens i where i.licitacao_id=l.id) as ultima_observacao
                    from glp.licitacoes l join lateral
                        (select id, captura, coletado_em from glp.licitacao_versoes where licitacao_id=l.id
                         order by numero_versao desc limit 1) v on true
                    where l.fonte_id=%s and l.id_externo=any(%s)''',
                    (self.fontes[source.fonte], ids)).fetchall() if ids else []
                original, versions, items, counts, rejects = planejar(source.registros, source.fonte,
                    self.fontes[source.fonte], state['id'], {r['id_externo']: r for r in known})
                self.inserir_lote('licitacoes', original)
                self.inserir_lote('licitacao_versoes', versions)
                self.inserir_lote('consulta_itens', items)
                errors = list(source.erros)
                status = source.status
                if rejects:
                    errors.append(erro('CONTRATO_DADOS', f'{len(rejects)} registros rejeitados; revisar relatório local'))
                    status = 'PARCIAL' if items else 'FALHA'
                if errors and status == 'SUCESSO':
                    status = 'PARCIAL'
                self.execute('''update glp.consultas_fontes set status=%s, tem_erro=%s, erros=%s,
                    iniciada_em=%s, finalizada_em=%s, recebidos=%s, selecionados=%s, requisicoes=%s,
                    novos=%s, alterados=%s, inalterados=%s, rejeitados=%s where id=%s''',
                    (status, bool(errors), self.Jsonb(errors), source.iniciada_em, source.finalizada_em,
                     source.recebidos, len(source.registros), source.requisicoes, counts['novos'],
                     counts['alterados'], counts['inalterados'], counts['rejeitados'], state['id']))
            return {'status': status, **counts, 'erros': errors, 'rejeicoes': rejects, 'ignorada': False}
        except Exception as exc:
            # A transação da fonte falha inteira; as outras fontes continuam independentes.
            errors = list(source.erros) + [erro('PERSISTENCIA_INTERNA',
                'Falha ao persistir o lote; transação da fonte revertida', tipo=type(exc).__name__,
                sqlstate=getattr(exc, 'sqlstate', None))]
            self.execute('''update glp.consultas_fontes set status='FALHA', tem_erro=true, erros=%s,
                novos=0, alterados=0, inalterados=0, rejeitados=0, selecionados=%s where id=%s''',
                (self.Jsonb(errors), len(source.registros), state['id']))
            return {'status': 'FALHA', 'novos': 0, 'alterados': 0, 'inalterados': 0,
                    'rejeitados': 0, 'erros': errors, 'ignorada': False}

    def finalizar(self, cid, results, historica=False):
        values = list(results.values())
        if all(s.get('ignorada') for s in values) and str(cid) in self.consultas_finalizadas:
            return 'JA_PROCESSADA'
        issues = any(s['status'] in ('FALHA', 'PARCIAL') for s in values)
        useful = any(s['status'] in ('SUCESSO', 'DESCONHECIDO') or
            (s.get('novos') or 0) + (s.get('alterados') or 0) + (s.get('inalterados') or 0) > 0 for s in values)
        status = ('PARCIAL' if useful else 'FALHA') if issues else 'SUCESSO'
        summary = 'Importação histórica; cobertura original não comprovada.' if historica else 'Coleta processada por fonte.'
        # O início usa o relógio do servidor; o término deve usar o mesmo relógio.
        self.execute('update glp.consultas set status=%s, finalizada_em=clock_timestamp(), mensagem_resumo=%s where id=%s',
                     (status, summary, cid))
        return status
