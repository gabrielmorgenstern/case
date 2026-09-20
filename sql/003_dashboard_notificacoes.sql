-- Operações mínimas do dashboard sobre a fila de Telegram.
begin;

create or replace view glp.historico_revisoes with (security_invoker = true) as
select r.id as revisao_id, r.sequencia, r.decisao, r.observacao, r.criada_em,
       r.usuario_id, u.nome_exibicao as revisor, v.id as versao_id, v.numero_versao,
       l.id as licitacao_id, l.id_externo, f.codigo as fonte,
       v.orgao, v.municipio, v.objeto,
       exists (select 1 from glp.licitacao_versoes posterior
               where posterior.licitacao_id = l.id and posterior.numero_versao > v.numero_versao)
           as possui_versao_posterior
from glp.revisoes r
join glp.usuarios u on u.id = r.usuario_id
join glp.licitacao_versoes v on v.id = r.versao_id
join glp.licitacoes l on l.id = v.licitacao_id
join glp.fontes f on f.id = v.fonte_id;

create or replace function glp.listar_minhas_notificacoes(p_limite integer default 100)
returns table (
    revisao_id uuid, notificacao_id uuid, status text, tentativas integer,
    criada_em timestamptz, atualizada_em timestamptz, enviada_em timestamptz,
    ultimo_erro jsonb, licitacao_id uuid, versao_id uuid, fonte text,
    orgao text, municipio text, objeto text, valor_estimado numeric,
    encerramento_propostas timestamptz
)
language sql stable security definer set search_path = ''
as $$
    select r.id, n.id, n.status, n.tentativas, n.criada_em, n.atualizada_em, n.enviada_em,
           n.ultimo_erro, v.licitacao_id, v.id, f.codigo, v.orgao, v.municipio, v.objeto,
           v.valor_estimado, v.encerramento_propostas
    from glp.notificacoes n
    join glp.revisoes r on r.id = n.revisao_id
    join glp.licitacao_versoes v on v.id = r.versao_id
    join glp.fontes f on f.id = v.fonte_id
    where glp_privado.usuario_ativo() and r.usuario_id = auth.uid()
    order by n.criada_em desc
    limit least(greatest(coalesce(p_limite, 100), 1), 200);
$$;

create or replace function glp.registrar_resultado_notificacao(
    p_revisao_id uuid, p_status text, p_id_mensagem text, p_erro text
) returns text
language plpgsql security definer set search_path = ''
as $$
declare
    fila glp.notificacoes%rowtype;
begin
    if not glp_privado.usuario_ativo() then
        raise exception 'Usuário não autorizado.' using errcode = '42501';
    end if;
    if p_status is null or p_status not in ('ENVIADA', 'FALHA', 'RESULTADO_INCERTO') then
        raise exception 'Resultado de notificação inválido.' using errcode = '22023';
    end if;
    select n.* into strict fila
    from glp.notificacoes n join glp.revisoes r on r.id = n.revisao_id
    where n.revisao_id = p_revisao_id and r.usuario_id = auth.uid()
    for update of n;
    if fila.status = 'ENVIADA' then
        return fila.status;
    end if;
    if p_status = 'ENVIADA' and (p_id_mensagem is null or btrim(p_id_mensagem) = '') then
        raise exception 'Envio confirmado exige identificador.' using errcode = '22023';
    end if;
    update glp.notificacoes set
        status = p_status,
        tentativas = tentativas + 1,
        atualizada_em = clock_timestamp(),
        proxima_tentativa_em = case when p_status = 'FALHA' then clock_timestamp() + interval '5 minutes' end,
        bloqueada_ate = null,
        enviada_em = case when p_status = 'ENVIADA' then clock_timestamp() end,
        id_mensagem_provedor = case when p_status = 'ENVIADA' then p_id_mensagem end,
        ultimo_erro = case when p_status = 'ENVIADA' then null else
            jsonb_build_object('categoria', p_status, 'mensagem', left(coalesce(p_erro, 'Erro não detalhado'), 500),
                               'horario', clock_timestamp()) end
    where id = fila.id;
    return p_status;
end;
$$;

revoke all on function glp.listar_minhas_notificacoes(integer) from public, anon, authenticated, service_role;
revoke all on function glp.registrar_resultado_notificacao(uuid, text, text, text) from public, anon, authenticated, service_role;
grant execute on function glp.listar_minhas_notificacoes(integer) to authenticated;
grant execute on function glp.registrar_resultado_notificacao(uuid, text, text, text) to authenticated;
grant select on glp.historico_revisoes to authenticated;

comment on function glp.listar_minhas_notificacoes(integer) is
    'Lista somente notificações geradas por revisões do usuário autenticado.';
comment on function glp.registrar_resultado_notificacao(uuid, text, text, text) is
    'Registra o resultado do Telegram somente para revisão do usuário autenticado.';

commit;
