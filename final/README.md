# Case GLP — coleta e carga no Supabase

Coleta licitações públicas no PNCP e no TCE-RJ, aplica os filtros do coletor existente e grava os resultados no banco do case. Esta pasta contém o pacote de coleta e ingestão; publique **o conteúdo desta pasta como raiz do repositório**.

**Comece pelo [guia de publicação e configuração](docs/GUIA_GITHUB.md).**

## Fluxo

`GitHub Actions → integrar.py coletar → PNCP / TCE-RJ → tratamento → Supabase`

- Agenda de hora em hora, no minuto 17, após ativar `COLETA_ATIVA=true`.
- Fontes processadas independentemente: uma falha na API não impede a gravação da outra.
- Novas licitações são inseridas; alterações geram versões, preservando a captura original.
- Registros inalterados não geram novas versões. A consulta mantém contadores e histórico.
- Uma conexão por execução, consultas em lote e bloqueio contra ingestões simultâneas.
- Por padrão, até 80 chamadas e orçamento de 300 segundos por fonte, com intervalo mínimo de 1 segundo. Retentativas também consomem o orçamento. Esses limites podem produzir resultados parciais e não garantem cobertura completa.
- A chave padrão permite uma coleta por hora UTC; fontes já finalizadas nessa chave são ignoradas em nova execução.

O banco e o usuário restrito de ingestão precisam estar provisionados. O banco definitivo deste case já foi preparado; o workflow não cria tabelas nem executa migrações.

## Arquivos

| Arquivo/pasta | Uso |
| --- | --- |
| `integrar.py` | Entrada da coleta e da carga |
| `teste.py` | Coletor original reutilizado, preservado |
| `coleta_integrada.py` | Orquestração das APIs, limites e erros por fonte |
| `integracao_dados.py` | Normalização e comparação de registros |
| `integracao_banco.py` | Persistência em lotes no Supabase |
| `requirements.txt`, `.python-version` | Dependências e Python |
| `.github/workflows/coleta.yml` | Agendamento e execução manual |
| `.github/workflows/testes.yml` | Testes sem consultar APIs ou banco |
| `scripts/resumo_actions.py` | Resumo dos resultados no GitHub Actions |
| `tests/` | Testes com dados e conexões simulados |
| `.env.example`, `.gitignore` | Modelo de configuração e exclusões |
| `docs/GUIA_GITHUB.md` | Publicação, secrets, execução e diagnóstico |

## Execução local

Com Python 3.12, dentro desta pasta:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Preencha `.env.coleta.local` conforme o guia. Depois:

```powershell
# Consulta as APIs e grava no banco.
.\.venv\Scripts\python.exe integrar.py coletar

# Consulta as APIs, sem acessar o banco.
.\.venv\Scripts\python.exe integrar.py coletar --sem-banco

# Testes locais, sem APIs e sem banco.
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

Executar somente `python integrar.py` mostra a ajuda: o subcomando `coletar` é obrigatório. Executar `teste.py` diretamente não faz a carga no banco.

As pastas `dados/coletas/` e `relatorios/` são criadas automaticamente e ficam fora do Git. Os códigos de saída são: `0` concluído sem falha de fonte; `1` erro geral; `2` alguma fonte parcial ou com falha. No GitHub, uma execução parcial fica vermelha para tornar a ocorrência visível, mesmo que a outra fonte tenha gravado com sucesso.

Dashboard, login, revisão e envio de Telegram são etapas posteriores e não são executados por este pacote. Nenhum token do Telegram é necessário nesta automação.
