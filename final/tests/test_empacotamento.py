import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import integrar
from integracao_dados import ResultadoFonte
from scripts.resumo_actions import resumo


class EmpacotamentoTest(unittest.TestCase):
    def test_checkout_novo_cria_checkpoint_e_preserva_carga_da_fonte_valida(self):
        import coleta_integrada
        import integracao_banco

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            job = SimpleNamespace(chave='teste', origem='AGENDADA',
                fontes=[ResultadoFonte('PNCP'), ResultadoFonte('TCE_RJ')])
            states = {f: {'id': f, 'status': 'EM_EXECUCAO'} for f in ('PNCP', 'TCE_RJ')}
            sources = [ResultadoFonte('PNCP', [], 'FALHA'),
                       ResultadoFonte('TCE_RJ', [{'id_oportunidade': 'teste'}], 'SUCESSO')]
            db = Mock(comandos=5)
            db.iniciar.return_value = ('consulta', states)
            db.pendente.return_value = True
            db.salvar_fonte.side_effect = [{'status': 'FALHA'}, {'status': 'SUCESSO', 'novos': 1}]
            db.finalizar.return_value = 'PARCIAL'
            with patch.object(integrar, 'ROOT', root), \
                 patch.object(sys, 'argv', ['integrar.py', 'coletar']), \
                 patch.object(coleta_integrada, 'preparar_execucao', return_value=job), \
                 patch.object(coleta_integrada, 'coletar_fonte', side_effect=sources), \
                 patch.object(integracao_banco, 'Banco', return_value=db), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(integrar.main(), 2)
            self.assertEqual(db.salvar_fonte.call_count, 2)
            checkpoints = list((root / 'dados' / 'coletas').glob('*.jsonl'))
            self.assertEqual(len(checkpoints), 2)
            captured = [json.loads(line) for file in checkpoints for line in file.read_text().splitlines()]
            self.assertEqual(captured, [{'id_oportunidade': 'teste'}])
            report = json.loads(next((root / 'relatorios').glob('*.json')).read_text())
            self.assertEqual(report['execucoes'][0]['status'], 'PARCIAL')
            db.close.assert_called_once()

    def test_resumo_exibe_contadores_sem_publicar_conteudo_da_api(self):
        text = resumo({'execucoes': [{'fontes': {'PNCP': {
            'status': 'PARCIAL', 'novos': 2, 'registros': ['CONTEUDO_EXTERNO'],
            'erros': [{'mensagem': 'TEXTO_NAO_PUBLICADO'}]}}}]})
        self.assertIn('| PNCP | PARCIAL | 2 |', text)
        self.assertNotIn('CONTEUDO_EXTERNO', text)
        self.assertNotIn('TEXTO_NAO_PUBLICADO', text)


if __name__ == '__main__':
    unittest.main()
