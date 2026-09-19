"""Resumo do relatório local; sem rede, banco ou impressão de credenciais."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def resumo(report):
    lines = ['## Coleta GLP', '',
             '| Fonte | Status | Novos | Alterados | Inalterados | Rejeitados |',
             '| --- | --- | ---: | ---: | ---: | ---: |']
    for execution in report.get('execucoes', []):
        for name, source in execution.get('fontes', {}).items():
            # Somente valores internos conhecidos e contadores; sem conteúdo vindo das APIs.
            name = name if name in ('PNCP', 'TCE_RJ') else 'Outra'
            status = source.get('status', '')
            if status not in ('SUCESSO', 'PARCIAL', 'FALHA', 'DESCONHECIDO', 'EM_EXECUCAO'):
                status = 'Indisponível'
            counts = [str(int(source.get(k, 0))) for k in ('novos', 'alterados', 'inalterados', 'rejeitados')]
            lines.append('| ' + ' | '.join([name, status, *counts]) + ' |')
    lines += ['', 'Código 2 indica coleta parcial ou falha em alguma fonte. '
              'Os dados gravados pela outra fonte permanecem no banco. '
              'Consulte as categorias de erro no relatório para distinguir API, limite local e persistência.', '']
    return '\n'.join(lines)


def main():
    files = sorted((ROOT / 'relatorios').glob('integracao_*.json'))
    content = resumo(json.loads(files[-1].read_text(encoding='utf-8'))) if files else (
        '## Coleta GLP\n\nNão foi produzido relatório. Consulte o passo que falhou nos logs.\n')
    destination = os.environ.get('GITHUB_STEP_SUMMARY')
    if destination:
        with open(destination, 'a', encoding='utf-8') as file:
            file.write(content)
    else:
        print(content)


if __name__ == '__main__':
    main()
