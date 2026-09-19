import copy
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integracao_dados import normalizar_registro, planejar, ResultadoFonte
from integracao_banco import Banco


def registro():
    return {'fonte': 'TCE_RJ', 'id_oportunidade': 'TCE_RJ:teste',
            'coletado_em': '2026-09-18T12:00:00Z', 'valor_estimado': 100,
            'situacao': 'SEM_HOMOLOGACAO_REGISTRADA', 'data_homologacao': '2026-05-01',
            'data_publicacao_pncp': '2026-04-01', 'srp': True}


class PlanejamentoTest(unittest.TestCase):
    def test_original_preservado_e_inconsistencia(self):
        raw = registro()
        before = copy.deepcopy(raw)
        row = normalizar_registro(raw, 'TCE_RJ')
        self.assertEqual(raw, before)
        self.assertIsNone(row['data_publicacao_pncp'])
        self.assertEqual(str(row['data_publicacao_edital']), '2026-04-01')
        self.assertTrue(any(w['campo'] == 'situacao' for w in row['inconsistencias']))

    def test_horario_sem_fuso_nao_inventado(self):
        raw = {**registro(), 'fonte': 'PNCP', 'data_publicacao_pncp': '2026-04-01T12:00:00'}
        row = normalizar_registro(raw, 'PNCP')
        self.assertIsNone(row['data_publicacao_pncp'])
        self.assertTrue(any(w['campo'] == 'data_publicacao_pncp' for w in row['inconsistencias']))

    def test_captura_sem_fuso_rejeitada(self):
        raw = {**registro(), 'coletado_em': '2026-09-18T12:00:00'}
        result = planejar([raw], 'TCE_RJ', 'f', 'c', {})
        self.assertEqual(result[3]['rejeitados'], 1)
        self.assertFalse(result[0])

    def test_mudanca_apenas_metadados(self):
        old = registro()
        raw = {**old, 'coletado_em': '2026-09-18T13:00:00Z', 'motivo_revisao': 'Falha PNCP'}
        known = {old['id_oportunidade']: {'licitacao_id': 'l', 'versao_id': 'v', 'captura': old,
            'coletado_em': datetime(2026, 9, 18, 12, tzinfo=timezone.utc)}}
        result = planejar([raw], 'TCE_RJ', 'f', 'c', known)
        self.assertEqual(result[3]['inalterados'], 1)
        self.assertFalse(result[1])
        self.assertEqual(result[2][0]['versao_id'], 'v')

    def test_alteracao_preserva_original(self):
        old = registro()
        raw = {**old, 'coletado_em': '2026-09-18T13:00:00Z', 'valor_estimado': 200}
        known = {old['id_oportunidade']: {'licitacao_id': 'l', 'versao_id': 'v', 'captura': old,
            'coletado_em': datetime(2026, 9, 18, 12, tzinfo=timezone.utc)}}
        result = planejar([raw], 'TCE_RJ', 'f', 'c', known)
        self.assertEqual(result[3]['alterados'], 1)
        self.assertFalse(result[0])
        self.assertEqual(old['valor_estimado'], 100)

    def test_historico_fora_de_ordem_rejeitado(self):
        old = registro()
        known = {old['id_oportunidade']: {'licitacao_id': 'l', 'versao_id': 'v', 'captura': old,
            'coletado_em': datetime(2026, 9, 18, 13, tzinfo=timezone.utc)}}
        result = planejar([old], 'TCE_RJ', 'f', 'c', known)
        self.assertEqual(result[3]['rejeitados'], 1)

    def test_identificador_ausente_nao_inventado(self):
        raw = {**registro(), 'id_oportunidade': ''}
        result = planejar([raw], 'TCE_RJ', 'f', 'c', {})
        self.assertEqual(result[3]['rejeitados'], 1)

    def test_lote_usa_uma_leitura_e_tres_insercoes(self):
        from contextlib import nullcontext
        db = Banco.__new__(Banco)
        db.conn = Mock()
        db.conn.transaction.side_effect = nullcontext
        db.fontes = {'TCE_RJ': 'f'}
        db.execute = Mock(return_value=Mock(fetchall=lambda: []))
        db.inserir_lote = Mock()
        db.Jsonb = lambda x: x
        rows = [{**registro(), 'id_oportunidade': f'TCE_RJ:{i}'} for i in range(100)]
        out = db.salvar_fonte(ResultadoFonte('TCE_RJ', rows, 'SUCESSO'),
                             {'id': 'c', 'status': 'EM_EXECUCAO'})
        self.assertEqual(out['novos'], 100)
        self.assertEqual(db.execute.call_count, 2)  # leitura + atualização dos contadores
        self.assertEqual(db.inserir_lote.call_count, 3)

    def test_fonte_finalizada_nao_consulta_novamente(self):
        db = Banco.__new__(Banco)
        db.execute = Mock()
        out = db.salvar_fonte(ResultadoFonte('TCE_RJ'), {'status': 'SUCESSO', 'erros': []})
        self.assertTrue(out['ignorada'])
        db.execute.assert_not_called()

    def test_retomada_finaliza_consulta_com_relogio_do_banco(self):
        db = Banco.__new__(Banco)
        db.consultas_finalizadas = set()
        db.execute = Mock()
        state = {'TCE_RJ': {'status': 'DESCONHECIDO', 'novos': 18, 'ignorada': True}}
        self.assertEqual(db.finalizar('c', state, historica=True), 'SUCESSO')
        query, params = db.execute.call_args.args
        self.assertIn('clock_timestamp()', query)
        self.assertEqual(len(params), 3)

    def test_reimportacao_finalizada_nao_escreve(self):
        db = Banco.__new__(Banco)
        db.consultas_finalizadas = {'c'}
        db.execute = Mock()
        self.assertEqual(db.finalizar('c', {'TCE_RJ': {'status': 'SUCESSO', 'ignorada': True}}), 'JA_PROCESSADA')
        db.execute.assert_not_called()


class ColetaTest(unittest.TestCase):
    def test_pncp_entrega_pagina_antes_da_falha_seguinte(self):
        import coleta_integrada as c
        with patch.object(c.original, 'requisitar', side_effect=[Mock(), c.LimiteColeta('limite')]), \
             patch.object(c.original, 'json_da_resposta', return_value={'data': [{'numeroControlePNCP': 'teste'}], 'totalPaginas': 2}):
            pages = c.paginas_pncp(Mock(), 'https://exemplo.invalid', '20260101', '20260102', {})
            self.assertEqual(next(pages), [{'numeroControlePNCP': 'teste'}])
            with self.assertRaises(c.LimiteColeta):
                next(pages)

    def test_orcamento_interrompe_chamadas(self):
        from coleta_integrada import SessaoLimitada, LimiteColeta
        base = Mock()
        base.get.return_value.status_code = 204
        s = SessaoLimitada(SimpleNamespace(segundos_fonte=10, max_chamadas_fonte=1,
                            intervalo_requisicoes=0), base=base, clock=lambda: 0, sleep=lambda _: None)
        s.get('https://exemplo.invalid')
        with self.assertRaises(LimiteColeta):
            s.get('https://exemplo.invalid')
        self.assertEqual(base.get.call_count, 1)

    def test_tce_preserva_pagina_valida_antes_da_falha(self):
        import coleta_integrada as c
        args = SimpleNamespace(segundos_fonte=10, max_chamadas_fonte=2, intervalo_requisicoes=0,
            ano_tce=2026, municipio_tce=None, incluir_revisar=False)
        items = [{'Ente': 'TESTE', 'ProcessoLicitatorio': str(i), 'NumeroEdital': str(i),
                  'Ano': 2026, 'Objeto': 'aquisição de GLP'} for i in range(500)]
        session = Mock(chamadas=2, falhas={})
        with patch.object(c, 'SessaoLimitada', return_value=session), \
             patch.object(c.original, 'requisitar', side_effect=[Mock(), c.LimiteColeta('limite')]), \
             patch.object(c.original, 'json_da_resposta', return_value={'Licitacoes': items}):
            out = c.coletar_fonte('TCE_RJ', args)
        self.assertEqual(out.status, 'PARCIAL')
        self.assertEqual(len(out.registros), 500)
        self.assertEqual(out.erros[0]['categoria'], 'LIMITE_LOCAL')


if __name__ == '__main__':
    unittest.main()
