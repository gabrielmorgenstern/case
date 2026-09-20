"""Dados fictícios somente para inspeção visual local explícita."""
from datetime import datetime, timedelta, timezone

NOW = datetime.now(timezone.utc)


def oportunidades():
    bases = [
        ('TCE_RJ', 'Itaperuna', 'Prefeitura de Itaperuna', 'Aquisição de gás liquefeito de petróleo para unidades municipais', 148500, 'A_REVISAR', True),
        ('TCE_RJ', 'Nova Friburgo', 'Município de Nova Friburgo', 'Fornecimento parcelado de GLP P-13 e P-45', 96200, 'VALIDADA', False),
        ('PNCP', 'Rio de Janeiro', 'Instituto Federal', 'Contratação de empresa para fornecimento de GLP', None, 'PRECISA_INFORMACAO', False),
        ('TCE_RJ', 'Sapucaia', 'Prefeitura de Sapucaia', 'Registro de preços para aquisição de gás de cozinha', 73500, 'DESCARTADA', False),
        ('PNCP', 'Niterói', 'Fundação Municipal', 'Fornecimento contínuo de gás liquefeito', 211000, 'A_REVISAR', False),
    ]
    return [{
        'licitacao_id': f'00000000-0000-4000-8000-00000000000{i}',
        'versao_id': f'10000000-0000-4000-8000-00000000000{i}', 'numero_versao': 2 if changed else 1,
        'id_externo': f'{source}:PREVIA-{i}', 'fonte': source, 'municipio': city, 'orgao': org,
        'objeto': obj, 'valor_estimado': value, 'modalidade': 'Pregão eletrônico',
        'unidade_administrativa': 'Secretaria de Administração', 'cnpj_orgao': '00.000.000/0001-00',
        'uf': 'RJ', 'codigo_ibge_municipio': f'33000{i}', 'esfera': 'Municipal',
        'poder': 'Executivo', 'modo_disputa': 'Aberto', 'srp': True,
        'numero_compra': f'{100 + i}/2026', 'processo': f'PROC-{i:03d}/2026',
        'ano_compra': 2026, 'sequencial_compra': str(i),
        'classificacao_glp': 'GLP_CONFIRMADO', 'status_comercial': 'OPORTUNIDADE',
        'score_glp': 95, 'termos_fortes': ['GLP', 'gás liquefeito de petróleo'],
        'termos_suporte': ['botijão'], 'termos_exclusao': [],
        'situacao': 'Em andamento', 'abertura_propostas': None,
        'encerramento_propostas': (NOW + timedelta(days=i + 2)).isoformat(),
        'data_publicacao_pncp': (NOW - timedelta(days=i)).isoformat(),
        'data_publicacao_edital': (NOW - timedelta(days=i)).date().isoformat(),
        'data_publicacao_oficial': None, 'data_homologacao': None,
        'informacao_complementar': 'Entrega parcelada conforme demanda do órgão.',
        'coletado_em': (NOW - timedelta(hours=i)).isoformat(),
        'registrada_em': (NOW - timedelta(hours=i)).isoformat(),
        'versao_coletada_em': (NOW - timedelta(hours=i)).isoformat(),
        'campos_alterados': {'valor_estimado': {'anterior': 130000, 'novo': value}} if changed else {},
        'inconsistencias': [], 'status_revisao': status, 'ultima_revisao_id': None,
        'revisor_id': None, 'revisada_em': None, 'alteracao_pendente': changed,
        'alterada_apos_revisao': changed, 'ultima_observacao_em': NOW.isoformat(),
    } for i, (source, city, org, obj, value, status, changed) in enumerate(bases, 1)]


def coletas():
    return [
        {'id': '1', 'iniciada_em': NOW.isoformat(), 'finalizada_em': NOW.isoformat(),
         'origem_execucao': 'MANUAL', 'status': 'PARCIAL', 'fontes_planejadas': 2,
         'fontes_completas': 1, 'fontes_com_erro': 1, 'novos_conhecidos': 4,
         'alterados_conhecidos': 0, 'inalterados_conhecidos': 16, 'teve_novidades': True},
        {'id': '2', 'iniciada_em': (NOW - timedelta(hours=6)).isoformat(),
         'finalizada_em': (NOW - timedelta(hours=6)).isoformat(), 'origem_execucao': 'AGENDADA',
         'status': 'SUCESSO', 'fontes_planejadas': 2, 'fontes_completas': 2,
         'fontes_com_erro': 0, 'novos_conhecidos': 0, 'alterados_conhecidos': 1,
         'inalterados_conhecidos': 24, 'teve_novidades': True},
    ]
