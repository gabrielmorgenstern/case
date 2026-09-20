"""Painel operacional de licitações GLP."""
from __future__ import annotations

import html
import os
import time
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import pandas as pd
import streamlit as st

from dashboard.client import AcessoNegado, ServicoIndisponivel, Supabase
from dashboard.preview import coletas as preview_coletas
from dashboard.preview import oportunidades as preview_oportunidades
from dashboard.telegram import enviar, montar_mensagem
from dashboard.viewmodels import STATUS_LABEL, data_br, filtrar, metricas, moeda

ROOT = Path(__file__).resolve().parent
PREVIEW = os.getenv('GLP_LOCAL_PREVIEW') == '1'

st.set_page_config(page_title='Radar GLP', page_icon='◆', layout='wide',
                   initial_sidebar_state='expanded')
st.markdown(f'<style>{(ROOT / "assets" / "style.css").read_text(encoding="utf-8")}</style>',
            unsafe_allow_html=True)


def segredo(nome, padrao=''):
    try:
        return st.secrets.get(nome, padrao)
    except FileNotFoundError:
        return padrao


def cliente() -> Supabase:
    url, key = segredo('SUPABASE_URL'), segredo('SUPABASE_ANON_KEY')
    if not url or not key:
        raise ServicoIndisponivel('Configure SUPABASE_URL e SUPABASE_ANON_KEY nos Secrets do Streamlit.')
    return Supabase(url, key)


def sessao_atual():
    sessao = st.session_state.get('sessao')
    if sessao and sessao.expira_em <= time.time() + 60:
        st.session_state.sessao = cliente().renovar(sessao)
        sessao = st.session_state.sessao
    return sessao


def limpar_cache():
    st.session_state.pop('dados_cache', None)
    st.session_state.pop('revisoes_cache', None)


def carregar_dados(forcar=False):
    if PREVIEW:
        return preview_oportunidades(), preview_coletas()
    cached = st.session_state.get('dados_cache')
    if cached and not forcar and time.time() - cached['em'] < 300:
        return cached['oportunidades'], cached['coletas']
    api, session = cliente(), sessao_atual()
    opportunities = api.selecionar(session, 'painel_licitacoes', params={
        'select': '*', 'order': 'ultima_observacao_em.desc.nullslast'}, limite=1000)
    collections = api.selecionar(session, 'resumo_consultas', params={
        'select': '*', 'order': 'iniciada_em.desc'}, limite=50)
    st.session_state.dados_cache = {'em': time.time(), 'oportunidades': opportunities,
                                    'coletas': collections}
    return opportunities, collections


def carregar_revisoes():
    if PREVIEW:
        return [{
            'decisao': 'VALIDADA', 'fonte': 'TCE_RJ', 'orgao': 'Município de Nova Friburgo',
            'municipio': 'Nova Friburgo', 'objeto': 'Fornecimento parcelado de GLP',
            'revisor': 'Prévia local', 'criada_em': pd.Timestamp.now('UTC').isoformat(),
            'numero_versao': 1, 'possui_versao_posterior': False}]
    cached = st.session_state.get('revisoes_cache')
    if cached and time.time() - cached['em'] < 300:
        return cached['linhas']
    rows = cliente().selecionar(sessao_atual(), 'historico_revisoes', params={
        'select': '*', 'order': 'sequencia.desc'}, limite=500)
    st.session_state.revisoes_cache = {'em': time.time(), 'linhas': rows}
    return rows


def titulo(secao, titulo_texto, descricao):
    st.markdown(f'<div class="eyebrow">{secao}</div><div class="hero-title">{titulo_texto}</div>'
                f'<div class="hero-copy">{descricao}</div>', unsafe_allow_html=True)
    st.write('')


def card_metrica(rotulo, valor, nota=''):
    st.markdown(f'<div class="metric-card"><div class="metric-label">{html.escape(rotulo)}</div>'
                f'<div class="metric-value">{html.escape(str(valor))}</div>'
                f'<div class="metric-note">{html.escape(nota)}</div></div>', unsafe_allow_html=True)


def tela_login():
    st.markdown('<div class="login-shell"><div class="login-mark">GLP</div>'
                '<div class="eyebrow">INTELIGÊNCIA COMERCIAL</div>'
                '<div class="login-title">Radar de licitações</div>'
                '<div class="login-copy">Acesso restrito ao painel operacional.</div></div>',
                unsafe_allow_html=True)
    if PREVIEW:
        st.session_state.usuario = {'nome_exibicao': 'Prévia local', 'ativo': True}
        st.session_state.sessao = True
        st.rerun()
    _, center, _ = st.columns([1, 1.15, 1])
    with center, st.form('login'):
        email = st.text_input('E-mail', placeholder='nome@empresa.com')
        password = st.text_input('Senha', type='password')
        submitted = st.form_submit_button('Entrar', width='stretch')
    if submitted:
        try:
            api = cliente()
            session = api.entrar(email.strip(), password)
            user = api.usuario_ativo(session)
            if not user:
                api.sair(session)
                raise AcessoNegado('Usuário não autorizado para este painel.')
            st.session_state.sessao, st.session_state.usuario = session, user
            st.rerun()
        except (AcessoNegado, ServicoIndisponivel) as exc:
            st.error(str(exc))


def barra_lateral():
    user = st.session_state.get('usuario', {})
    with st.sidebar:
        st.markdown('<div class="brand">PROATIVA<span class="brand-dot">◆</span> RADAR GLP</div>',
                    unsafe_allow_html=True)
        st.caption('Inteligência operacional')
        st.write('')
        page = st.radio('Navegação', ['Visão geral', 'Fila de revisão', 'Histórico de revisões',
                                      'Histórico de coletas', 'Notificações'], label_visibility='collapsed')
        st.write('')
        if st.button('↻ Atualizar dados', width='stretch'):
            limpar_cache()
            st.rerun()
        cached = st.session_state.get('dados_cache')
        if cached:
            st.caption('Cache ativo por 5 minutos. Use o botão somente quando precisar de dados imediatos.')
        st.write('')
        st.caption(f"Conectado como\n{user.get('nome_exibicao') or getattr(st.session_state.get('sessao'), 'email', 'Usuário')}")
        if st.button('Sair', width='stretch'):
            if not PREVIEW:
                try:
                    cliente().sair(st.session_state.sessao)
                except Exception:
                    pass
            for key in ('sessao', 'usuario', 'dados_cache'):
                st.session_state.pop(key, None)
            st.rerun()
    return page


def status_ultima_coleta(collections):
    if not collections:
        st.info('Ainda não há coletas registradas.')
        return
    last = collections[0]
    status = STATUS_LABEL.get(last.get('status'), last.get('status', 'Indisponível'))
    st.markdown('<div class="status-strip"><span class="status-dot"></span>'
                f'<strong>Última coleta: {html.escape(status)}</strong><br>'
                f'<span>{data_br(last.get("iniciada_em"), True)} · '
                f'{int(last.get("novos_conhecidos") or 0)} novas · '
                f'{int(last.get("alterados_conhecidos") or 0)} alteradas · '
                f'{int(last.get("fontes_com_erro") or 0)} fonte(s) com erro</span></div>',
                unsafe_allow_html=True)


def visao_geral(rows, collections):
    titulo('01 · VISÃO GERAL', 'Decisões com <em>contexto</em>.',
           'Acompanhe oportunidades, revisões e a saúde das fontes públicas em um só lugar.')
    numbers = metricas(rows)
    cols = st.columns(5)
    cards = [('Total', numbers['total'], 'oportunidades monitoradas'),
             ('A revisar', numbers['A_REVISAR'], 'na fila operacional'),
             ('Validadas', numbers['VALIDADA'], 'aprovadas pelo time'),
             ('Alteradas', numbers['alteradas'], 'mudanças pendentes'),
             ('Valor mapeado', moeda(numbers['valor_total']), 'somente valores informados')]
    for col, card in zip(cols, cards):
        with col:
            card_metrica(*card)
    st.write('')
    status_ultima_coleta(collections)
    st.write('')
    left, right = st.columns([1, 1])
    frame = pd.DataFrame(rows)
    with left:
        st.markdown('<div class="section-title">Oportunidades por fonte</div>', unsafe_allow_html=True)
        if not frame.empty:
            chart = frame.groupby('fonte').size().rename('Licitações')
            st.bar_chart(chart, color='#B89B62', horizontal=True, height=260)
        else:
            st.info('Sem dados para exibir.')
    with right:
        st.markdown('<div class="section-title">Municípios com mais oportunidades</div>', unsafe_allow_html=True)
        if not frame.empty:
            chart = frame.assign(municipio=frame.municipio.fillna('Não informado')).groupby(
                'municipio').size().sort_values(ascending=False).head(8).rename('Licitações')
            st.bar_chart(chart, color='#071124', horizontal=True, height=260)
        else:
            st.info('Sem dados para exibir.')


def tabela_oportunidades(rows):
    table = pd.DataFrame([{
        'Situação': 'Alterada' if r.get('alteracao_pendente') else 'Nova/monitorada',
        'Revisão': STATUS_LABEL.get(r.get('status_revisao'), r.get('status_revisao')),
        'Fonte': r.get('fonte'), 'Município': r.get('municipio') or 'Não informado',
        'Órgão': r.get('orgao') or 'Não informado', 'Objeto': r.get('objeto') or 'Não informado',
        'Valor': moeda(r.get('valor_estimado')), 'Prazo': data_br(r.get('encerramento_propostas')),
        '_id': r.get('licitacao_id')}
        for r in rows])
    if table.empty:
        st.info('Nenhuma licitação corresponde aos filtros.')
        return None
    event = st.dataframe(table.drop(columns=['_id']), hide_index=True, width='stretch',
                         height=min(520, 90 + 35 * len(table)), on_select='rerun',
                         selection_mode='single-row')
    indexes = event.selection.rows
    return rows[indexes[0]] if indexes else None


def filtros(rows):
    with st.container(border=True):
        a, b, c = st.columns([1.4, 1, 1])
        search = a.text_input('Buscar', placeholder='Objeto, órgão, município ou processo')
        statuses = b.multiselect('Status', ['A_REVISAR', 'VALIDADA', 'DESCARTADA', 'PRECISA_INFORMACAO'],
                                 format_func=lambda x: STATUS_LABEL[x])
        sources = c.multiselect('Fonte', sorted({r.get('fonte') for r in rows if r.get('fonte')}))
        d, e = st.columns([2, 1])
        cities = d.multiselect('Município', sorted({r.get('municipio') for r in rows if r.get('municipio')}))
        changed = e.toggle('Somente alteradas')
    return filtrar(rows, search, statuses, sources, cities, changed)


def link_seguro(url):
    if not url:
        return False
    parsed = urlparse(str(url))
    return parsed.scheme == 'https' and bool(parsed.netloc)


def texto_campo(valor):
    if valor is None or valor == '':
        return 'Não informado'
    if isinstance(valor, bool):
        return 'Sim' if valor else 'Não'
    if isinstance(valor, (list, tuple)):
        return ', '.join(str(item) for item in valor) if valor else 'Não informado'
    return str(valor)


def codigo_legivel(valor):
    if valor in (None, ''):
        return 'Não informado'
    texto = str(valor).strip()
    if texto == 'TCE_RJ':
        return 'TCE-RJ'
    if '_' in texto or texto.isupper():
        texto = texto.replace('_', ' ').lower().capitalize()
        texto = texto.replace('Glp', 'GLP').replace('Pncp', 'PNCP')
    return texto


def campo_detalhe(rotulo, valor):
    st.markdown(
        f'<div class="detail-field"><div class="detail-label">{html.escape(rotulo)}</div>'
        f'<div class="detail-value">{html.escape(texto_campo(valor))}</div></div>',
        unsafe_allow_html=True,
    )


def grupo_detalhes(titulo_grupo, campos, colunas=4):
    st.markdown(f'<div class="detail-group-title">{html.escape(titulo_grupo)}</div>',
                unsafe_allow_html=True)
    cols = st.columns(colunas)
    for indice, (rotulo, valor) in enumerate(campos):
        with cols[indice % colunas]:
            campo_detalhe(rotulo, valor)


def carregar_detalhes(row):
    if PREVIEW:
        return [row], []
    api, session = cliente(), sessao_atual()
    versions = api.selecionar(session, 'licitacao_versoes', params={
        'select': '*', 'licitacao_id': f'eq.{row["licitacao_id"]}', 'order': 'numero_versao.desc'}, limite=50)
    revision = api.selecionar(session, 'revisoes', params={
        'select': '*', 'versao_id': f'in.({",".join(v["id"] for v in versions)})',
        'order': 'sequencia.desc'}, limite=100) if versions else []
    return versions, revision


def registrar_resultado_telegram(revisao_id, result):
    if result.status == 'PENDENTE':
        return
    cliente().rpc(sessao_atual(), 'registrar_resultado_notificacao', {
        'p_revisao_id': revisao_id, 'p_status': result.status,
        'p_id_mensagem': result.id_mensagem, 'p_erro': result.erro})


def notificacao_da_revisao(revisao_id):
    rows = cliente().rpc(sessao_atual(), 'listar_minhas_notificacoes', {'p_limite': 200}) or []
    return next((row for row in rows if str(row.get('revisao_id')) == str(revisao_id)), None)


def tentar_notificar(revisao_id, item):
    telegram = segredo('telegram', {})
    result = enviar(telegram.get('BOT_TOKEN', ''), telegram.get('CHAT_ID', ''),
                    montar_mensagem(item, st.session_state.usuario.get('nome_exibicao', '')))
    if not PREVIEW:
        registrar_resultado_telegram(revisao_id, result)
    return result


@st.dialog('Registrar decisão')
def dialogo_revisao(row):
    labels = {'Validar': 'VALIDADA', 'Precisa de informação': 'PRECISA_INFORMACAO',
              'Descartar': 'DESCARTADA'}
    with st.form('decisao'):
        choice = st.radio('Decisão', list(labels), horizontal=True)
        note = st.text_area('Observação', max_chars=2000,
                            placeholder='Contexto opcional para o histórico da revisão')
        confirmed = st.checkbox('Confirmo a decisão para esta versão da licitação.')
        submitted = st.form_submit_button('Registrar decisão', width='stretch')
    if submitted:
        if not confirmed:
            st.warning('Confirme a decisão antes de continuar.')
            return
        if PREVIEW:
            st.success('Prévia: decisão simulada, sem gravação.')
            return
        try:
            revision_id = cliente().rpc(sessao_atual(), 'registrar_revisao', {
                'p_versao_id': row['versao_id'], 'p_decisao': labels[choice],
                'p_observacao': note.strip() or None, 'p_chave_operacao': str(uuid4())})
            limpar_cache()
            if labels[choice] == 'VALIDADA':
                notification = notificacao_da_revisao(str(revision_id))
                if not notification:
                    st.success('Validação registrada. Esta versão já estava validada; nenhuma nova notificação foi criada.')
                else:
                    result = tentar_notificar(str(revision_id), {**row, **notification})
                    if result.status == 'ENVIADA':
                        st.success('Licitação validada e Telegram enviado.')
                    elif result.status == 'PENDENTE':
                        st.warning('Validação registrada. Telegram ainda não configurado; envio pendente.')
                    else:
                        st.warning(f'Validação registrada. {result.erro}')
            else:
                st.success('Decisão registrada no histórico.')
        except (AcessoNegado, ServicoIndisponivel) as exc:
            st.error(str(exc))


def detalhes(row):
    versions, revisions = carregar_detalhes(row)
    current = versions[0] if versions else row
    st.divider()
    c1, c2 = st.columns([3, 1])
    with c1:
        st.markdown('<div class="eyebrow">DETALHES DA OPORTUNIDADE</div>', unsafe_allow_html=True)
        st.subheader(current.get('objeto') or 'Objeto não informado')
        st.caption(f"{row.get('fonte')} · {current.get('municipio') or 'Município não informado'} · versão {current.get('numero_versao', 1)}")
    with c2:
        if st.button('Registrar revisão', type='primary', width='stretch'):
            dialogo_revisao(row)
    a, b, c, d = st.columns(4)
    a.metric('Órgão', current.get('orgao') or 'Não informado')
    b.metric('Valor estimado', moeda(current.get('valor_estimado')))
    c.metric('Encerramento', data_br(current.get('encerramento_propostas')))
    d.metric('Status', STATUS_LABEL.get(row.get('status_revisao'), row.get('status_revisao')))
    with st.expander('Informações completas da licitação', expanded=True):
        grupo_detalhes('Identificação', [
            ('Fonte', codigo_legivel(row.get('fonte'))),
            ('Identificador externo', row.get('id_externo')),
            ('Número da compra', current.get('numero_compra')),
            ('Processo', current.get('processo')),
            ('Ano da compra', current.get('ano_compra')),
            ('Sequencial da compra', current.get('sequencial_compra')),
        ], 3)
        grupo_detalhes('Órgão e localização', [
            ('Órgão', current.get('orgao')),
            ('Unidade administrativa', current.get('unidade_administrativa')),
            ('CNPJ do órgão', current.get('cnpj_orgao')),
            ('Município', current.get('municipio')),
            ('UF', current.get('uf')),
            ('Código IBGE', current.get('codigo_ibge_municipio')),
            ('Esfera', codigo_legivel(current.get('esfera'))),
            ('Poder', codigo_legivel(current.get('poder'))),
        ])
        grupo_detalhes('Características do processo', [
            ('Modalidade', codigo_legivel(current.get('modalidade'))),
            ('Modo de disputa', codigo_legivel(current.get('modo_disputa'))),
            ('Sistema de registro de preços', current.get('srp')),
            ('Situação', codigo_legivel(current.get('situacao'))),
            ('Status comercial', codigo_legivel(current.get('status_comercial'))),
            ('Valor estimado', moeda(current.get('valor_estimado'))),
        ], 3)
        grupo_detalhes('Datas', [
            ('Publicação no PNCP', data_br(current.get('data_publicacao_pncp'), True)),
            ('Publicação do edital', data_br(current.get('data_publicacao_edital'))),
            ('Publicação oficial', data_br(current.get('data_publicacao_oficial'))),
            ('Abertura das propostas', data_br(current.get('abertura_propostas'), True)),
            ('Encerramento das propostas', data_br(current.get('encerramento_propostas'), True)),
            ('Homologação', data_br(current.get('data_homologacao'))),
        ], 3)
        grupo_detalhes('Classificação GLP', [
            ('Classificação', codigo_legivel(current.get('classificacao_glp'))),
            ('Pontuação', current.get('score_glp')),
            ('Termos fortes', current.get('termos_fortes')),
            ('Termos de suporte', current.get('termos_suporte')),
            ('Termos de exclusão', current.get('termos_exclusao')),
        ], 3)
        grupo_detalhes('Rastreabilidade', [
            ('Versão', current.get('numero_versao')),
            ('Coletada em', data_br(current.get('coletado_em') or row.get('versao_coletada_em'), True)),
            ('Registrada em', data_br(current.get('registrada_em'), True)),
            ('Última observação', data_br(row.get('ultima_observacao_em'), True)),
            ('Referência oficial', current.get('referencia_publicacao_oficial')),
        ], 3)
        if current.get('informacao_complementar'):
            st.markdown('<div class="detail-group-title">Informação complementar</div>',
                        unsafe_allow_html=True)
            campo_detalhe('Descrição', current.get('informacao_complementar'))
        links = [(label, current.get(key)) for label, key in [
            ('Abrir no PNCP', 'link_pncp'),
            ('Abrir sistema de origem', 'link_sistema_origem'),
            ('Abrir processo eletrônico', 'link_processo_eletronico'),
        ] if link_seguro(current.get(key))]
        if links:
            st.markdown('<div class="detail-group-title">Acessos externos</div>',
                        unsafe_allow_html=True)
            cols = st.columns(len(links))
            for col, (label, url) in zip(cols, links):
                col.link_button(label, url, width='stretch')
        if current.get('inconsistencias'):
            st.markdown('<div class="detail-group-title">Qualidade dos dados</div>',
                        unsafe_allow_html=True)
            inconsistencias = current['inconsistencias']
            if isinstance(inconsistencias, list):
                st.dataframe(pd.DataFrame(inconsistencias), hide_index=True, width='stretch')
            else:
                campo_detalhe('Inconsistências', inconsistencias)
    if current.get('campos_alterados'):
        with st.expander('Alterações detectadas', expanded=True):
            changes = [{'Campo': key, 'Valor anterior': values.get('anterior'), 'Valor atual': values.get('novo')}
                       for key, values in current['campos_alterados'].items()]
            st.dataframe(changes, hide_index=True, width='stretch')
    with st.expander('Histórico de versões e revisões'):
        if revisions:
            st.dataframe(pd.DataFrame(revisions)[['decisao', 'observacao', 'criada_em']].rename(columns={
                'decisao': 'Decisão', 'observacao': 'Observação', 'criada_em': 'Registrada em'}),
                hide_index=True, width='stretch')
        else:
            st.caption('Ainda não há revisão registrada para esta oportunidade.')


def fila_revisao(rows):
    titulo('02 · OPERAÇÃO', 'Fila de <em>revisão</em>.',
           'Filtre as oportunidades, examine a versão atual e registre uma decisão auditável.')
    selected_rows = filtros(rows)
    st.caption(f'{len(selected_rows)} resultado(s). Selecione uma linha para abrir os detalhes.')
    selected = tabela_oportunidades(selected_rows)
    if selected:
        detalhes(selected)
    st.info('A revisão é manual neste case. Este fluxo pode ser automatizado futuramente com análise assistida por IA.')


def historico_revisoes():
    titulo('03 · GOVERNANÇA', 'Histórico de <em>decisões</em>.',
           'Acompanhe o estado atual e preserve a rastreabilidade das revisões realizadas.')
    revised = carregar_revisoes()
    table = [{
        'Decisão': STATUS_LABEL.get(r.get('decisao'), r.get('decisao')),
        'Fonte': r.get('fonte'), 'Órgão': r.get('orgao'), 'Município': r.get('municipio'),
        'Objeto': r.get('objeto'), 'Versão': r.get('numero_versao'), 'Revisor': r.get('revisor'),
        'Observação': r.get('observacao'), 'Revisada em': data_br(r.get('criada_em'), True),
        'Mudança posterior': 'Sim' if r.get('possui_versao_posterior') else 'Não'} for r in revised]
    if table:
        st.dataframe(table, hide_index=True, width='stretch')
    else:
        st.info('Nenhuma revisão registrada para as versões atuais.')


def historico_coletas(collections):
    titulo('04 · CONFIABILIDADE', 'Saúde das <em>coletas</em>.',
           'Resultados separados por execução, sem confundir indisponibilidade de fonte com falha do sistema.')
    table = [{
        'Início': data_br(r.get('iniciada_em'), True),
        'Origem': r.get('origem_execucao'), 'Status': STATUS_LABEL.get(r.get('status'), r.get('status')),
        'Fontes completas': int(r.get('fontes_completas') or 0),
        'Fontes com erro': int(r.get('fontes_com_erro') or 0),
        'Novas': int(r.get('novos_conhecidos') or 0),
        'Alteradas': int(r.get('alterados_conhecidos') or 0),
        'Inalteradas': int(r.get('inalterados_conhecidos') or 0)} for r in collections]
    if table:
        st.dataframe(table, hide_index=True, width='stretch')
    else:
        st.info('Nenhuma coleta registrada.')


def notificacoes():
    titulo('05 · ALERTAS', 'Envios pelo <em>Telegram</em>.',
           'A validação permanece salva mesmo quando o canal de notificação apresenta falha.')
    if PREVIEW:
        rows = [{'revisao_id': 'demo', 'status': 'ENVIADA', 'tentativas': 1,
                 'orgao': 'Município de Nova Friburgo', 'municipio': 'Nova Friburgo',
                 'objeto': 'Fornecimento parcelado de GLP', 'criada_em': pd.Timestamp.now('UTC').isoformat()}]
    else:
        try:
            rows = cliente().rpc(sessao_atual(), 'listar_minhas_notificacoes', {'p_limite': 100}) or []
        except (AcessoNegado, ServicoIndisponivel) as exc:
            st.error(str(exc))
            return
    if not rows:
        st.info('Nenhuma notificação gerada por este usuário.')
        return
    for item in rows:
        with st.container(border=True):
            a, b, c = st.columns([3, 1, 1])
            a.markdown(f"**{item.get('orgao') or 'Órgão não informado'}**  \n{item.get('objeto') or 'Objeto não informado'}")
            b.metric('Telegram', STATUS_LABEL.get(item.get('status'), item.get('status')))
            c.metric('Tentativas', int(item.get('tentativas') or 0))
            pode_reenviar = item.get('status') in ('PENDENTE', 'FALHA')
            if item.get('status') == 'RESULTADO_INCERTO':
                st.warning('O envio pode ter ocorrido. Confira a conversa antes de qualquer nova tentativa.')
                pode_reenviar = st.checkbox(
                    'Confirmo que a mensagem não chegou.',
                    key=f"confirm-retry-{item['revisao_id']}",
                )
            if pode_reenviar and st.button(
                    'Tentar envio novamente' if item.get('status') == 'RESULTADO_INCERTO' else 'Tentar envio',
                    key=f"retry-{item['revisao_id']}"):
                result = tentar_notificar(item['revisao_id'], item)
                if result.status == 'ENVIADA':
                    st.success('Mensagem enviada.')
                else:
                    st.warning(result.erro)
                st.rerun()


def main():
    if 'sessao' not in st.session_state:
        tela_login()
        return
    if PREVIEW:
        st.markdown('<div class="preview-banner">Prévia visual local: nenhum banco, autenticação ou Telegram é acessado.</div>',
                    unsafe_allow_html=True)
    try:
        page = barra_lateral()
        rows, collections = carregar_dados()
        if page == 'Visão geral':
            visao_geral(rows, collections)
        elif page == 'Fila de revisão':
            fila_revisao(rows)
        elif page == 'Histórico de revisões':
            historico_revisoes()
        elif page == 'Histórico de coletas':
            historico_coletas(collections)
        else:
            notificacoes()
    except AcessoNegado as exc:
        st.session_state.pop('sessao', None)
        st.error(str(exc))
    except ServicoIndisponivel as exc:
        st.error(str(exc))


if __name__ == '__main__':
    main()
