from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PNCP_BASE_URL = "https://pncp.gov.br/api/consulta/v1"
PNCP_PROPOSTA_URL = f"{PNCP_BASE_URL}/contratacoes/proposta"
PNCP_PUBLICACAO_URL = f"{PNCP_BASE_URL}/contratacoes/publicacao"
PNCP_DETALHE_URL = f"{PNCP_BASE_URL}/orgaos/{{cnpj}}/compras/{{ano}}/{{sequencial}}"
TCE_BASE_URL = "https://dados.tcerj.tc.br/api/v1"
TCE_LICITACOES_URL = f"{TCE_BASE_URL}/licitacoes"
# A API do PNCP exige uma modalidade na consulta. A lista é deliberadamente
# configurável porque o catálogo pode ganhar novos códigos ao longo do tempo.
MODALIDADES_PNCP = tuple(range(1, 10))

TERMOS_FORTES = (
    "glp",
    "gas liquefeito de petroleo",
    "gas liquefeito",
    "gas de cozinha",
    "fornecimento de glp",
    "fornecimento de gas liquefeito",
    "aquisicao de glp",
    "aquisicao de gas liquefeito",
    "recarga de botijao",
    "recarga botijao",
)
TERMOS_SUPORTE = (
    "botijao", "botijoes", "vasilhame", "p13", "p20", "p45", "p90", "p190",
    "recarga de gas", "entrega de gas", "cilindro de gas",
)
TERMOS_EXCLUSAO = (
    "gas medicinal", "oxigenio medicinal", "oxigenio", "oxido nitroso", "nitrogenio",
    "gas natural", "gnv", "gasolina", "diesel", "etanol",
)

COLUNAS = [
    "fonte", "status_comercial", "classificacao_glp", "score_glp", "municipio",
    "orgao", "unidade_administrativa", "objeto", "encerramento_propostas",
    "abertura_propostas", "valor_estimado", "modalidade", "modo_disputa", "situacao",
    "srp", "numero_compra", "processo", "cnpj_orgao", "id_oportunidade", "link_pncp",
    "link_sistema_origem", "link_processo_eletronico", "referencia_publicacao_oficial",
    "motivo_revisao", "termos_fortes", "termos_suporte", "termos_exclusao",
    "data_publicacao_pncp", "data_publicacao_oficial", "data_homologacao",
    "codigo_ibge_municipio", "uf", "esfera", "poder", "ano_compra",
    "sequencial_compra", "informacao_complementar", "coletado_em",
]


class FonteIndisponivelError(RuntimeError):
    pass


def normalizar(valor: Any) -> str:
    texto = "" if valor is None else str(valor)
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.lower()
    texto = re.sub(r"[^a-z0-9]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def encontrar_termos(texto: str, termos: Iterable[str]) -> list[str]:
    return [termo for termo in termos if termo in texto]


def classificar_glp(objeto: Any = "", complemento: Any = "") -> dict[str, Any]:
    texto = normalizar(f"{objeto or ''} {complemento or ''}")
    fortes = encontrar_termos(texto, TERMOS_FORTES)
    suporte = encontrar_termos(texto, TERMOS_SUPORTE)
    exclusoes = encontrar_termos(texto, TERMOS_EXCLUSAO)
    score = len(fortes) * 5 + len(suporte) * 2 - len(exclusoes) * 6
    if fortes and not exclusoes:
        classificacao = "CONFIRMADO_GLP"
    elif score >= 3 and not exclusoes:
        classificacao = "PROVAVEL_GLP"
    elif fortes and exclusoes:
        classificacao = "REVISAR"
    else:
        classificacao = "NAO_GLP"
    return {
        "score_glp": score,
        "classificacao_glp": classificacao,
        "termos_fortes": ", ".join(fortes),
        "termos_suporte": ", ".join(suporte),
        "termos_exclusao": ", ".join(exclusoes),
    }


def criar_sessao() -> requests.Session:
    sessao = requests.Session()
    sessao.headers.update({
        "Accept": "application/json",
        "User-Agent": "Monitor-GLP-RJ/2.0 (dados publicos; contato-operacional)",
    })
    retry = Retry(
        total=0,
        connect=0,
        read=0,
        status=0,
        allowed_methods=frozenset({"GET"}),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    sessao.mount("https://", adapter)
    return sessao


def requisitar(
    sessao: requests.Session,
    url: str,
    params: dict[str, Any] | None,
    fonte: str,
    tentativas: int = 4,
    espera_base: float = 3.0,
) -> requests.Response:
    ultimo: Exception | None = None
    for tentativa in range(1, tentativas + 1):
        try:
            resposta = sessao.get(url, params=params or {}, timeout=(20, 120))
            if resposta.status_code == 204:
                return resposta
            if resposta.status_code == 429 or resposta.status_code >= 500:
                espera = espera_base * tentativa
                logging.warning("%s retornou HTTP %s; nova tentativa em %.1fs", fonte, resposta.status_code, espera)
                time.sleep(espera)
                continue
            resposta.raise_for_status()
            return resposta
        except (requests.Timeout, requests.ConnectionError) as erro:
            ultimo = erro
            espera = espera_base * tentativa
            logging.warning("Falha de rede em %s: %s; nova tentativa em %.1fs", fonte, erro, espera)
            time.sleep(espera)
    raise FonteIndisponivelError(f"{fonte} indisponível após {tentativas} tentativas: {ultimo}")


def json_da_resposta(resposta: requests.Response, fonte: str) -> dict[str, Any]:
    if resposta.status_code == 204 or not resposta.content:
        return {}
    try:
        payload = resposta.json()
    except ValueError as erro:
        raise FonteIndisponivelError(f"{fonte} não retornou JSON: {resposta.text[:300]}") from erro
    if not isinstance(payload, dict):
        raise FonteIndisponivelError(f"{fonte} retornou JSON em formato inesperado")
    return payload


def obter_paginas_pncp(
    sessao: requests.Session,
    endpoint: str,
    data_inicial: str,
    data_final: str,
    tamanho_pagina: int = 50,
    parametros_extras: dict[str, Any] | None = None,
    tentativas_requisicao: int = 2,
) -> list[dict[str, Any]]:
    if tamanho_pagina < 10 or tamanho_pagina > 50:
        raise ValueError("O PNCP documenta tamanhoPagina entre 10 e 50 para esta consulta.")
    registros: list[dict[str, Any]] = []
    pagina = 1
    while True:
        params = {
            "dataInicial": data_inicial,
            "dataFinal": data_final,
            "uf": "RJ",
            "pagina": pagina,
            "tamanhoPagina": tamanho_pagina,
        }
        params.update(parametros_extras or {})
        payload = json_da_resposta(
            requisitar(sessao, endpoint, params, "PNCP", tentativas=tentativas_requisicao),
            "PNCP",
        )
        lote = payload.get("data") or []
        if not isinstance(lote, list):
            raise FonteIndisponivelError("PNCP retornou campo data que não é lista")
        registros.extend(x for x in lote if isinstance(x, dict))
        total_paginas = int(payload.get("totalPaginas") or pagina)
        logging.info("PNCP %s página %s/%s: %s registros", endpoint.rsplit("/", 1)[-1], pagina, total_paginas, len(lote))
        if not lote or pagina >= total_paginas:
            break
        pagina += 1
        time.sleep(0.4)
    return registros


def obter_propostas_pncp(
    sessao: requests.Session,
    data_inicial: str,
    data_final: str,
    tamanho_pagina: int,
    max_falhas_transitorias: int = 2,
) -> list[dict[str, Any]]:
    registros: list[dict[str, Any]] = []
    erros: list[str] = []
    falhas_transitorias = 0
    for modalidade in MODALIDADES_PNCP:
        try:
            registros.extend(obter_paginas_pncp(
                sessao, PNCP_PROPOSTA_URL, data_inicial, data_final, tamanho_pagina,
                {"modalidade": modalidade},
            ))
            falhas_transitorias = 0
        except requests.HTTPError as erro:
            # Alguns códigos podem não estar habilitados no ambiente. Isso não
            # invalida as outras modalidades, portanto registramos e seguimos.
            erros.append(f"proposta/{modalidade}: {erro}")
            status = getattr(erro.response, "status_code", None) if erro.response is not None else None
            if status is not None and status >= 500:
                falhas_transitorias += 1
        except FonteIndisponivelError as erro:
            erros.append(f"proposta/{modalidade}: {erro}")
            falhas_transitorias += 1
        if falhas_transitorias >= max_falhas_transitorias:
            raise FonteIndisponivelError(
                f"PNCP interrompido após {falhas_transitorias} falhas transitórias consecutivas "
                "no endpoint de propostas; avançando para a próxima fonte."
            )
    if registros:
        return registros
    logging.warning("Nenhuma modalidade retornou propostas; tentando /publicacao")
    falhas_transitorias = 0
    for modalidade in MODALIDADES_PNCP:
        try:
            registros.extend(obter_paginas_pncp(
                sessao, PNCP_PUBLICACAO_URL, data_inicial, data_final, tamanho_pagina,
                {"codigoModalidadeContratacao": modalidade},
            ))
            falhas_transitorias = 0
        except requests.HTTPError as erro:
            erros.append(f"publicacao/{modalidade}: {erro}")
            status = getattr(erro.response, "status_code", None) if erro.response is not None else None
            if status is not None and status >= 500:
                falhas_transitorias += 1
        except FonteIndisponivelError as erro:
            erros.append(f"publicacao/{modalidade}: {erro}")
            falhas_transitorias += 1
        if falhas_transitorias >= max_falhas_transitorias:
            raise FonteIndisponivelError(
                f"PNCP interrompido após {falhas_transitorias} falhas transitórias consecutivas "
                "no endpoint de publicação; avançando para a próxima fonte."
            )
    if not registros and erros:
        raise FonteIndisponivelError("Nenhuma modalidade do PNCP pôde ser consultada; primeiro erro: " + erros[0])
    return registros


def obter_detalhe_pncp(sessao: requests.Session, resumo: dict[str, Any]) -> dict[str, Any]:
    orgao = resumo.get("orgaoEntidade") or {}
    cnpj = re.sub(r"\D", "", str(orgao.get("cnpj") or ""))
    ano = resumo.get("anoCompra")
    sequencial = resumo.get("sequencialCompra")
    if not (cnpj and ano and sequencial):
        return {}
    url = PNCP_DETALHE_URL.format(cnpj=cnpj, ano=int(ano), sequencial=int(sequencial))
    return json_da_resposta(requisitar(sessao, url, {}, "PNCP detalhe", tentativas=3), "PNCP detalhe")


def lista_tce(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for chave in ("Licitacoes", "licitacoes", "data", "dados", "items", "resultados", "results"):
            if isinstance(payload.get(chave), list):
                return [x for x in payload[chave] if isinstance(x, dict)]
    return []


def obter_licitacoes_tce(
    sessao: requests.Session,
    ano: int,
    municipio: str | None,
    limite: int,
) -> list[dict[str, Any]]:
    if limite < 1:
        raise ValueError("limite TCE deve ser positivo")
    resultados: list[dict[str, Any]] = []
    inicio = 0
    vistos: set[str] = set()
    while True:
        params: dict[str, Any] = {"ano": ano, "inicio": inicio, "limite": limite}
        if municipio:
            params["municipio"] = municipio
        payload = json_da_resposta(requisitar(sessao, TCE_LICITACOES_URL, params, "TCE-RJ"), "TCE-RJ")
        lote = lista_tce(payload)
        logging.info("TCE-RJ ano %s início %s: %s registros", ano, inicio, len(lote))
        if not lote:
            break
        novos = 0
        for item in lote:
            chave = "|".join(normalizar(item.get(c, "")) for c in ("Ente", "ProcessoLicitatorio", "NumeroEdital", "Ano", "Objeto"))
            if chave not in vistos:
                vistos.add(chave)
                resultados.append(item)
                novos += 1
        if len(lote) < limite or novos == 0:
            break
        inicio += len(lote)
        time.sleep(0.5)
    return resultados


def vazio(valor: Any) -> bool:
    return valor is None or str(valor).strip().lower() in {"", "null", "none", "nan", "nat"}


def prazo_aberto(valor: Any) -> bool:
    if vazio(valor):
        return True
    dt = pd.to_datetime(valor, errors="coerce", utc=True)
    return bool(pd.isna(dt) or dt > pd.Timestamp.now(tz="UTC"))


def url_valida(valor: Any) -> str:
    texto = "" if valor is None else str(valor).strip()
    return texto if texto.startswith(("http://", "https://")) else ""


def registro_pncp(resumo: dict[str, Any], detalhe: dict[str, Any], coletado_em: str) -> dict[str, Any]:
    compra = {**resumo, **detalhe}
    orgao = compra.get("orgaoEntidade") or {}
    unidade = compra.get("unidadeOrgao") or {}
    classificacao = classificar_glp(compra.get("objetoCompra"), compra.get("informacaoComplementar"))
    cnpj = re.sub(r"\D", "", str(orgao.get("cnpj") or ""))
    ano = compra.get("anoCompra")
    seq = compra.get("sequencialCompra")
    link = f"https://pncp.gov.br/app/editais/{cnpj}/{ano}/{seq}" if cnpj and ano and seq else ""
    return {
        "fonte": "PNCP", "status_comercial": "ABERTA_CONFIRMADA", "motivo_revisao": "",
        "id_oportunidade": compra.get("numeroControlePNCP"), "cnpj_orgao": cnpj,
        "orgao": orgao.get("razaoSocial"), "esfera": orgao.get("esferaId"), "poder": orgao.get("poderId"),
        "unidade_administrativa": unidade.get("nomeUnidade"), "municipio": unidade.get("municipioNome"),
        "codigo_ibge_municipio": unidade.get("codigoIbge"), "uf": unidade.get("ufSigla") or "RJ",
        "numero_compra": compra.get("numeroCompra"), "processo": compra.get("processo"),
        "ano_compra": ano, "sequencial_compra": seq, "modalidade": compra.get("modalidadeNome"),
        "modo_disputa": compra.get("modoDisputaNome"), "situacao": compra.get("situacaoCompraNome"),
        "srp": compra.get("srp"), "objeto": compra.get("objetoCompra"),
        "informacao_complementar": compra.get("informacaoComplementar"),
        "valor_estimado": compra.get("valorTotalEstimado"), "data_publicacao_pncp": compra.get("dataPublicacaoPncp"),
        "data_publicacao_oficial": "", "data_homologacao": "",
        "abertura_propostas": compra.get("dataAberturaProposta"),
        "encerramento_propostas": compra.get("dataEncerramentoProposta"),
        "referencia_publicacao_oficial": "", "link_sistema_origem": url_valida(compra.get("linkSistemaOrigem")),
        "link_processo_eletronico": url_valida(compra.get("linkProcessoEletronico")), "link_pncp": link,
        "coletado_em": coletado_em, **classificacao,
    }


def registro_tce(item: dict[str, Any], coletado_em: str, motivo: str) -> dict[str, Any]:
    objeto = item.get("Objeto") or ""
    classificacao = classificar_glp(objeto)
    chave = "|".join(normalizar(item.get(c, "")) for c in ("Ente", "ProcessoLicitatorio", "NumeroEdital", "Ano"))
    return {
        "fonte": "TCE_RJ", "status_comercial": "REVISAR_TCE_RJ",
        "motivo_revisao": "TCE-RJ não informa de forma confiável o prazo atual de propostas; validar edital. " + motivo,
        "id_oportunidade": f"TCE_RJ:{chave}", "cnpj_orgao": "", "orgao": item.get("Ente"),
        "esfera": "MUNICIPAL", "poder": "", "unidade_administrativa": item.get("Unidade"),
        "municipio": item.get("Ente"), "codigo_ibge_municipio": "", "uf": "RJ",
        "numero_compra": item.get("NumeroEdital"), "processo": item.get("ProcessoLicitatorio"),
        "ano_compra": item.get("Ano"), "sequencial_compra": "", "modalidade": item.get("Modalidade"),
        "modo_disputa": item.get("Tipo"), "situacao": "SEM_HOMOLOGACAO_REGISTRADA", "srp": "registro de preco" in normalizar(objeto),
        "objeto": objeto,
        "informacao_complementar": f"Adiado sine die: {item.get('AdiadoSineDie', '')}. Orçamento sigiloso: {item.get('OrcamentoSigiloso', '')}.",
        "valor_estimado": item.get("ValorEstimado"), "data_publicacao_pncp": item.get("DataPublicacaoEdital"),
        "data_publicacao_oficial": item.get("DataPublicacaoOficial"), "data_homologacao": item.get("DataHomologacao"),
        "abertura_propostas": "", "encerramento_propostas": "", "referencia_publicacao_oficial": item.get("PublicacaoOficial"),
        "link_sistema_origem": url_valida(item.get("PublicacaoOficial")), "link_processo_eletronico": "", "link_pncp": "",
        "coletado_em": coletado_em, **classificacao,
    }


def candidato(classificacao: dict[str, Any], incluir_revisar: bool) -> bool:
    return classificacao["classificacao_glp"] in {"CONFIRMADO_GLP", "PROVAVEL_GLP"} or (
        incluir_revisar and classificacao["classificacao_glp"] == "REVISAR"
    )


def deduplicar(registros: list[dict[str, Any]]) -> list[dict[str, Any]]:
    melhores: dict[str, dict[str, Any]] = {}
    for registro in registros:
        chave = str(registro.get("id_oportunidade") or "")
        if not chave:
            chave = "|".join(normalizar(registro.get(c, "")) for c in ("fonte", "orgao", "processo", "numero_compra", "objeto"))
        anterior = melhores.get(chave)
        if anterior is None or int(registro.get("score_glp") or 0) > int(anterior.get("score_glp") or 0):
            melhores[chave] = registro
    return list(melhores.values())


def salvar(registros: list[dict[str, Any]], saida: Path) -> tuple[Path, Path, Path]:
    dataframe = pd.DataFrame(registros).reindex(columns=COLUNAS)
    if not dataframe.empty:
        dataframe = dataframe.sort_values(
            ["status_comercial", "classificacao_glp", "score_glp", "encerramento_propostas"],
            ascending=[True, True, False, True], na_position="last",
        )
    saida.parent.mkdir(parents=True, exist_ok=True)
    csv = saida.with_suffix(".csv")
    jsonl = saida.with_suffix(".jsonl")
    dataframe.to_csv(csv, index=False, encoding="utf-8-sig")
    dataframe.to_excel(saida, index=False, sheet_name="Oportunidades GLP")
    with jsonl.open("w", encoding="utf-8") as arquivo:
        for registro in registros:
            arquivo.write(json.dumps(registro, ensure_ascii=False, default=str) + "\n")
    return saida, csv, jsonl


def intervalo_datas(dias: int) -> tuple[str, str]:
    fim = date.today()
    inicio = fim - timedelta(days=max(0, dias))
    return inicio.strftime("%Y%m%d"), fim.strftime("%Y%m%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extrator de oportunidades de GLP no RJ via PNCP e TCE-RJ")
    parser.add_argument("--dias-pncp", type=int, default=365, help="Janela de publicação do PNCP; padrão: 365 dias")
    parser.add_argument("--ano-inicial-tce", type=int, default=date.today().year, help="Primeiro ano do TCE-RJ")
    parser.add_argument("--ano-final-tce", type=int, default=date.today().year, help="Último ano do TCE-RJ")
    parser.add_argument("--municipio-tce", default=None, help="Opcional; sem este parâmetro consulta todos os municípios disponíveis")
    parser.add_argument("--limite-tce", type=int, default=500, help="Linhas por página no TCE-RJ")
    parser.add_argument("--tamanho-pagina-pncp", type=int, default=50, help="Linhas por página no PNCP, entre 10 e 50")
    parser.add_argument("--max-falhas-pncp", type=int, default=2, help="Falhas transitórias consecutivas antes de abandonar o PNCP e seguir para o TCE-RJ")
    parser.add_argument("--saida", default=f"dados/oportunidades_glp_rj_{date.today():%Y%m%d}.xlsx")
    parser.add_argument("--incluir-revisar", action="store_true")
    parser.add_argument("--sem-detalhes-pncp", action="store_true", help="Não chama um endpoint de detalhe por oportunidade")
    parser.add_argument("--somente-tce", action="store_true")
    parser.add_argument("--somente-pncp", action="store_true")
    parser.add_argument("--teste-conexao", action="store_true", help="Testa as duas APIs e encerra sem gerar dados")
    args = parser.parse_args()
    if args.somente_tce and args.somente_pncp:
        parser.error("--somente-tce e --somente-pncp são mutuamente exclusivos")
    if args.ano_inicial_tce > args.ano_final_tce:
        parser.error("ano inicial do TCE não pode ser maior que o ano final")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sessao = criar_sessao()
    data_inicial, data_final = intervalo_datas(args.dias_pncp)
    if args.teste_conexao:
        ok = True
        for nome, url, params in [
            ("PNCP", PNCP_PUBLICACAO_URL, {"dataInicial": data_inicial, "dataFinal": data_final, "uf": "RJ", "pagina": 1, "tamanhoPagina": 10, "codigoModalidadeContratacao": 1}),
            ("TCE-RJ", TCE_LICITACOES_URL, {"ano": args.ano_final_tce, "inicio": 0, "limite": 1}),
        ]:
            try:
                resposta = requisitar(sessao, url, params, nome, tentativas=2)
                logging.info("%s OK: HTTP %s", nome, resposta.status_code)
            except Exception as erro:
                ok = False
                logging.error("%s FALHOU: %s", nome, erro)
        return 0 if ok else 2

    coletado_em = datetime.now(timezone.utc).isoformat()
    registros: list[dict[str, Any]] = []
    pncp_erro = ""
    if not args.somente_tce:
        try:
            resumos = obter_propostas_pncp(
                sessao,
                data_inicial,
                data_final,
                args.tamanho_pagina_pncp,
                max_falhas_transitorias=args.max_falhas_pncp,
            )
            logging.info("PNCP retornou %s registros no período %s–%s", len(resumos), data_inicial, data_final)
            for indice, resumo in enumerate(resumos, 1):
                classificacao = classificar_glp(resumo.get("objetoCompra"), resumo.get("informacaoComplementar"))
                if not candidato(classificacao, args.incluir_revisar):
                    continue
                detalhe = {}
                if not args.sem_detalhes_pncp:
                    try:
                        detalhe = obter_detalhe_pncp(sessao, resumo)
                    except Exception as erro:
                        logging.warning("Detalhe PNCP indisponível no registro %s: %s", indice, erro)
                item = registro_pncp(resumo, detalhe, coletado_em)
                if prazo_aberto(item.get("encerramento_propostas")):
                    registros.append(item)
        except Exception as erro:
            pncp_erro = str(erro)
            logging.error("PNCP indisponível: %s", erro)

    if not args.somente_pncp:
        for ano in range(args.ano_inicial_tce, args.ano_final_tce + 1):
            try:
                itens = obter_licitacoes_tce(sessao, ano, args.municipio_tce, args.limite_tce)
                logging.info("TCE-RJ retornou %s registros no ano %s", len(itens), ano)
                for item in itens:
                    classificacao = classificar_glp(item.get("Objeto"))
                    if candidato(classificacao, args.incluir_revisar):
                        registros.append(registro_tce(item, coletado_em, "Fonte complementar ao PNCP." if not pncp_erro else "PNCP indisponível: " + pncp_erro))
            except Exception as erro:
                logging.error("TCE-RJ indisponível no ano %s: %s", ano, erro)

    registros = deduplicar(registros)
    arquivos = salvar(registros, Path(args.saida))
    logging.info("Extração concluída: %s oportunidades únicas", len(registros))
    for arquivo in arquivos:
        logging.info("Arquivo gerado: %s", arquivo.resolve())
    return 0


if __name__ == "__main__":
    sys.exit(main())
