import csv
import io
import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from decimal import Decimal, InvalidOperation
import secrets

from flask import Blueprint, Response, jsonify, request, session

# A Flora guarda recebimento e saída na mesma linha (tabela in_out) e tem datas
# digitadas fora do padrão. A limpeza já existe no api_flora.py; reaproveitamos
# para os números do consolidado baterem com o dashboard da própria Flora.
from api_flora import BASE as FLORA_BASE, EXPEDIDAS as FLORA_EXPEDIDAS


api_4log = Blueprint("api_4log", __name__)

CAMINHO = Path(__file__).resolve().parent
PASTA_DB = CAMINHO / "db"

# As colunas de pedidos/notas variam entre as bases dos clientes.
# "estoque_inicial" (opcional, padrão 0): pallets que já estavam no estoque antes
# da primeira data da base. Ele entra no cálculo do pico de estoque (cobrança).
CLIENTES = [
    {
        "chave": "porto",
        "nome": "Porto-Brasil",
        "arquivo": "dados_base_porto.db",
        "pedido_recebimento": "num_pedido",
        "pedido_expedicao": "num_pedido",
    },
    {
        "chave": "sebastian",
        "nome": "Sebastian",
        "arquivo": "dados_base_sebastian.db",
        "pedido_recebimento": "nota_de_transferencia",
        "pedido_expedicao": "pedido",
    },
    {
        "chave": "flora",
        "nome": "Flora",
        "arquivo": "dados_base_flora.db",
        "tipo": "in_out",  # estrutura diferente: ver _resumo_flora
    },
]


@api_4log.before_request
def proteger_api_4log():
    """Somente usuários autenticados na empresa 4Log podem consultar o consolidado."""
    if "usuario" not in session:
        return jsonify(erro="não autenticado"), 401

    if session.get("empresa") != "4log":
        return jsonify(erro="sem permissão"), 403


def _filtro_data(ano, mes, coluna="data"):
    """Monta filtros de ano/mês com parâmetros SQL, evitando interpolar valores."""
    partes = []
    parametros = []

    if ano:
        partes.append(f"strftime('%Y', {coluna}) = ?")
        parametros.append(ano)

    if mes:
        partes.append(f"strftime('%m', {coluna}) = ?")
        parametros.append(mes)

    if not partes:
        return "", parametros

    return " AND " + " AND ".join(partes), parametros


def _tabela_existe(conn, tabela):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (tabela,),
    ).fetchone() is not None


def _colunas(conn, tabela):
    if not _tabela_existe(conn, tabela):
        return set()
    return {linha[1] for linha in conn.execute(f'PRAGMA table_info("{tabela}")')}


def _somar_pallets(conn, tabela, ano, mes):
    colunas = _colunas(conn, tabela)
    if not {"data", "qtd_pallet"}.issubset(colunas):
        return {
            "pallets": 0, "linhas": 0, "clientes": 0,
            "primeira_data": None, "ultima_data": None,
        }

    where, parametros = _filtro_data(ano, mes)
    tem_cliente = "cliente" in colunas
    cliente_expr = "cliente" if tem_cliente else "NULL"
    return conn.execute(
        f"""
        SELECT
            COALESCE(SUM(COALESCE(qtd_pallet, 0)), 0) AS pallets,
            COUNT(*) AS linhas,
            COUNT(DISTINCT {cliente_expr}) AS clientes,
            MIN(date(data)) AS primeira_data,
            MAX(date(data)) AS ultima_data
        FROM "{tabela}"
        WHERE 1=1 {where}
        """,
        parametros,
    ).fetchone()


def _contar_pedidos(conn, tabela, coluna_pedido, ano, mes):
    colunas = _colunas(conn, tabela)
    if "data" not in colunas or coluna_pedido not in colunas:
        return 0

    where, parametros = _filtro_data(ano, mes)
    return conn.execute(
        f"""
        SELECT COUNT(DISTINCT NULLIF(TRIM(CAST("{coluna_pedido}" AS TEXT)), ''))
        FROM "{tabela}"
        WHERE 1=1 {where}
        """,
        parametros,
    ).fetchone()[0] or 0


def _contar_clientes_finais(conn, ano, mes):
    partes = []
    parametros = []

    for tabela in ("recebimento", "expedicao"):
        colunas = _colunas(conn, tabela)
        if "data" not in colunas or "cliente" not in colunas:
            continue
        where, p = _filtro_data(ano, mes)
        partes.append(
            f"""SELECT TRIM(cliente) AS cliente
                FROM "{tabela}"
                WHERE cliente IS NOT NULL AND TRIM(cliente) <> '' {where}"""
        )
        parametros.extend(p)

    if not partes:
        return 0

    sql = "SELECT COUNT(DISTINCT cliente) FROM (" + " UNION ALL ".join(partes) + ")"
    return conn.execute(sql, parametros).fetchone()[0] or 0


def _pallets_por_dia(conn, tabela):
    """{data: pallets} de uma tabela com colunas data e qtd_pallet."""
    if not {"data", "qtd_pallet"}.issubset(_colunas(conn, tabela)):
        return {}
    linhas = conn.execute(
        f"""
        SELECT date(data) AS d, COALESCE(SUM(COALESCE(qtd_pallet, 0)), 0) AS p
        FROM "{tabela}"
        WHERE date(data) IS NOT NULL
        GROUP BY date(data)
        """
    ).fetchall()
    return {linha[0]: linha[1] for linha in linhas}


def _movimento_diario(conn, config):
    """Pallets recebidos e expedidos por dia: ({data: n}, {data: n})."""
    if config.get("tipo") == "in_out":
        rec = conn.execute(
            FLORA_BASE + """
            SELECT data_rec AS d, COALESCE(SUM(pallets), 0) AS p
            FROM b WHERE data_rec IS NOT NULL GROUP BY data_rec
            """
        ).fetchall()
        exp = conn.execute(
            FLORA_BASE + f"""
            SELECT data_saida AS d, COALESCE(SUM(pallets), 0) AS p
            FROM b WHERE {FLORA_EXPEDIDAS} GROUP BY data_saida
            """
        ).fetchall()
        return (
            {linha[0]: linha[1] for linha in rec},
            {linha[0]: linha[1] for linha in exp},
        )

    return (
        _pallets_por_dia(conn, "recebimento"),
        _pallets_por_dia(conn, "expedicao"),
    )


def _serie_estoque(conn, config):
    """
    Saldo de pallets em estoque ao FIM de cada dia com movimentação:
    estoque_inicial + recebidos acumulados - expedidos acumulados.
    O estoque só muda em dias de movimentação, então o pico sempre cai
    em um deles.
    """
    rec, exp = _movimento_diario(conn, config)
    saldo = int(config.get("estoque_inicial", 0))
    serie = []
    for dia in sorted(set(rec) | set(exp)):
        saldo += int(round(rec.get(dia, 0))) - int(round(exp.get(dia, 0)))
        serie.append((dia, saldo))
    return serie


def _meses_entre(primeiro, ultimo):
    """Lista 'AAAA-MM' de primeiro a ultimo, inclusive."""
    ano, mes = int(primeiro[:4]), int(primeiro[5:7])
    fim_ano, fim_mes = int(ultimo[:4]), int(ultimo[5:7])
    meses = []
    while (ano, mes) <= (fim_ano, fim_mes):
        meses.append(f"{ano:04d}-{mes:02d}")
        mes += 1
        if mes > 12:
            ano, mes = ano + 1, 1
    return meses


def _picos_mensais(serie, estoque_inicial, hoje=None):
    """
    Maior estoque de cada mês, do primeiro mês da base até o mês atual.

    O estoque que vem do mês anterior conta: se o mês abre com 344 pallets e
    o primeiro movimento é uma saída, o pico é 344 (dia 1º), não o saldo
    depois da saída. Meses sem movimentação entram se ainda havia estoque.
    """
    if not serie:
        return []

    primeiro = serie[0][0][:7]
    ultimo = max(serie[-1][0][:7], (hoje or date.today()).strftime("%Y-%m"))

    por_mes = {}
    for dia, saldo in serie:
        por_mes.setdefault(dia[:7], []).append((dia, saldo))

    resultado = []
    saldo_anterior = estoque_inicial
    for ym in _meses_entre(primeiro, ultimo):
        movimentos = por_mes.get(ym, [])
        candidatos = []
        if ym != primeiro:
            candidatos.append((ym + "-01", saldo_anterior))
        elif estoque_inicial > 0:
            candidatos.append((serie[0][0], estoque_inicial))
        candidatos.extend(movimentos)

        if not candidatos:
            continue
        if not movimentos and candidatos[0][1] <= 0:
            continue  # mês parado e sem estoque: nada a cobrar

        # max devolve o primeiro maior: em empate vale o dia mais antigo.
        dia, pallets = max(candidatos, key=lambda c: c[1])
        resultado.append({
            "mes": ym,
            "data": dia,
            "pallets": pallets,
            "sem_movimentacao": not movimentos,
        })
        if movimentos:
            saldo_anterior = movimentos[-1][1]

    return resultado


def _info_estoque(conn, config, ano, mes, hoje=None):
    """
    Pico de estoque do período filtrado e pico de cada mês.
    O saldo é acumulado desde o início da base; o filtro só recorta
    quais meses entram na busca pelo maior valor.
    """
    try:
        serie = _serie_estoque(conn, config)
    except sqlite3.Error:
        return {"pico_estoque": None, "pico_estoque_por_mes": []}

    mensais = [
        m for m in _picos_mensais(serie, int(config.get("estoque_inicial", 0)), hoje)
        if (not ano or m["mes"][:4] == ano) and (not mes or m["mes"][5:7] == mes)
    ]
    pico = max(mensais, key=lambda m: m["pallets"]) if mensais else None

    return {
        "pico_estoque": (
            {"data": pico["data"], "pallets": pico["pallets"]} if pico else None
        ),
        "pico_estoque_por_mes": sorted(
            mensais, key=lambda m: m["mes"], reverse=True
        ),
    }


def _resumo_flora(conn, ano, mes):
    """
    Resumo da Flora no mesmo formato dos demais clientes.

    - Recebimento: cargas filtradas por data de recebimento.
    - Expedição: cargas que já saíram (check_saida = 1 e data de saída válida),
      filtradas pela data de saída. Cada operação usa a sua própria data,
      igual ao api_flora.py.
    - "Pedido" na Flora = CT-e distinto (cada linha da base é uma carga/CT-e).
    """
    w_rec, p_rec = _filtro_data(ano, mes, "data_rec")
    w_exp, p_exp = _filtro_data(ano, mes, "data_saida")

    rec = conn.execute(
        FLORA_BASE + f"""
        SELECT COALESCE(SUM(pallets), 0)        AS pallets,
               COUNT(*)                         AS linhas,
               COUNT(DISTINCT NULLIF(cte, 0))   AS pedidos,
               MIN(data_rec)                    AS primeira_data,
               MAX(data_rec)                    AS ultima_data
        FROM b
        WHERE 1=1 {w_rec}
        """,
        p_rec,
    ).fetchone()

    exp = conn.execute(
        FLORA_BASE + f"""
        SELECT COALESCE(SUM(pallets), 0)        AS pallets,
               COUNT(*)                         AS linhas,
               COUNT(DISTINCT NULLIF(cte, 0))   AS pedidos,
               MIN(data_saida)                  AS primeira_data,
               MAX(data_saida)                  AS ultima_data
        FROM b
        WHERE {FLORA_EXPEDIDAS} {w_exp}
        """,
        p_exp,
    ).fetchone()

    clientes_finais = conn.execute(
        FLORA_BASE + f"""
        SELECT COUNT(DISTINCT cliente) FROM (
            SELECT cliente FROM b
            WHERE cliente IS NOT NULL AND cliente <> '' {w_rec}
            UNION ALL
            SELECT cliente FROM b
            WHERE cliente IS NOT NULL AND cliente <> ''
              AND {FLORA_EXPEDIDAS} {w_exp}
        )
        """,
        p_rec + p_exp,
    ).fetchone()[0] or 0

    recebido = int(rec["pallets"] or 0)
    expedido = int(exp["pallets"] or 0)
    linhas_rec = int(rec["linhas"] or 0)
    linhas_exp = int(exp["linhas"] or 0)
    pedidos_rec = int(rec["pedidos"] or 0)
    pedidos_exp = int(exp["pedidos"] or 0)

    datas = [
        d for d in (
            rec["primeira_data"], rec["ultima_data"],
            exp["primeira_data"], exp["ultima_data"],
        ) if d
    ]
    periodo = (
        f"{min(datas)} a {max(datas)}" if datas else "Sem dados no período"
    )

    return {
        "chave": "flora",
        "nome": "Flora",
        "recebido": recebido,
        "expedido": expedido,
        "saldo_movimentacao": recebido - expedido,
        "pedidos": pedidos_rec,
        "pedidos_recebimento": pedidos_rec,
        "pedidos_expedicao": pedidos_exp,
        "documentos_operacionais": pedidos_rec + pedidos_exp,
        "clientes_finais": int(clientes_finais),
        "movimentacoes": linhas_rec + linhas_exp,
        "registros_recebimento": linhas_rec,
        "registros_expedicao": linhas_exp,
        "periodo": periodo,
        "banco_disponivel": True,
    }


def _resumo_cliente(config, ano, mes):
    caminho = PASTA_DB / config["arquivo"]

    if not caminho.exists():
        return None

    conn = sqlite3.connect(caminho)
    conn.row_factory = sqlite3.Row

    try:
        if config.get("tipo") == "in_out":
            resumo = _resumo_flora(conn, ano, mes)
            resumo.update(_info_estoque(conn, config, ano, mes))
            return resumo

        recebimento = _somar_pallets(conn, "recebimento", ano, mes)
        expedicao = _somar_pallets(conn, "expedicao", ano, mes)

        recebido = int(recebimento["pallets"] or 0)
        expedido = int(expedicao["pallets"] or 0)
        linhas_recebimento = int(recebimento["linhas"] or 0)
        linhas_expedicao = int(expedicao["linhas"] or 0)

        datas = [
            d for d in (
                recebimento["primeira_data"],
                recebimento["ultima_data"],
                expedicao["primeira_data"],
                expedicao["ultima_data"],
            ) if d
        ]
        periodo = (
            f"{min(datas)} a {max(datas)}"
            if datas else "Sem dados no período"
        )

        # Contamos valores distintos separadamente em cada operação.
        # "pedidos" é mantido como compatibilidade e representa somente recebimento.
        # "documentos_operacionais" soma as duas operações; um mesmo número pode
        # aparecer em ambas e, nesse caso, será contabilizado em cada operação.
        pedidos_recebimento = int(_contar_pedidos(
            conn, "recebimento", config["pedido_recebimento"], ano, mes
        ))
        pedidos_expedicao = int(_contar_pedidos(
            conn, "expedicao", config["pedido_expedicao"], ano, mes
        ))

        resumo = {
            "chave": config["chave"],
            "nome": config["nome"],
            "recebido": recebido,
            "expedido": expedido,
            "saldo_movimentacao": recebido - expedido,
            "pedidos": pedidos_recebimento,
            "pedidos_recebimento": pedidos_recebimento,
            "pedidos_expedicao": pedidos_expedicao,
            "documentos_operacionais": pedidos_recebimento + pedidos_expedicao,
            "clientes_finais": int(_contar_clientes_finais(conn, ano, mes)),
            "movimentacoes": linhas_recebimento + linhas_expedicao,
            "registros_recebimento": linhas_recebimento,
            "registros_expedicao": linhas_expedicao,
            "periodo": periodo,
            "banco_disponivel": True,
        }
        resumo.update(_info_estoque(conn, config, ano, mes))
        return resumo
    finally:
        conn.close()


def _anos_disponiveis():
    anos = set()

    for config in CLIENTES:
        caminho = PASTA_DB / config["arquivo"]
        if not caminho.exists():
            continue

        conn = sqlite3.connect(caminho)
        try:
            if config.get("tipo") == "in_out":
                linhas = conn.execute(
                    FLORA_BASE + f"""
                    SELECT DISTINCT strftime('%Y', d) FROM (
                        SELECT data_rec AS d FROM b
                        UNION
                        SELECT data_saida FROM b WHERE {FLORA_EXPEDIDAS}
                    )
                    """
                ).fetchall()
                anos.update(str(linha[0]) for linha in linhas if linha[0])
                continue

            for tabela in ("recebimento", "expedicao"):
                colunas = _colunas(conn, tabela)
                if "data" not in colunas:
                    continue
                linhas = conn.execute(
                    f"""SELECT DISTINCT strftime('%Y', data)
                        FROM "{tabela}" WHERE data IS NOT NULL"""
                ).fetchall()
                anos.update(str(linha[0]) for linha in linhas if linha[0])
        except sqlite3.Error:
            # Uma base com estrutura incompleta não deve derrubar o resumo inteiro.
            continue
        finally:
            conn.close()

    return sorted(anos, reverse=True)


@api_4log.route("/api/4log/resumo-clientes")
def resumo_clientes():
    """Retorna os totais consolidados por cliente, filtrados por ano e mês."""
    ano = request.args.get("ano", "").strip()
    mes = request.args.get("mes", "").strip()

    if ano and (not ano.isdigit() or len(ano) != 4):
        return jsonify(erro="ano inválido"), 400

    if mes:
        if not mes.isdigit() or not 1 <= int(mes) <= 12:
            return jsonify(erro="mês inválido"), 400
        mes = mes.zfill(2)

    clientes = []
    for config in CLIENTES:
        try:
            resumo = _resumo_cliente(config, ano, mes)
            if resumo is not None:
                clientes.append(resumo)
        except sqlite3.Error:
            # Não expõe detalhes internos do banco na resposta HTTP.
            continue

    return jsonify({
        "clientes": clientes,
        "anos": _anos_disponiveis(),
        "filtros": {
            "ano": ano or None,
            "mes": mes or None,
        },
        "totais": {
            "clientes_com_base": len(clientes),
            "recebido": sum(c["recebido"] for c in clientes),
            "expedido": sum(c["expedido"] for c in clientes),
            # "pedidos" corresponde a recebimento, mantendo compatibilidade com o HTML atual.
            "pedidos": sum(c["pedidos_recebimento"] for c in clientes),
            "pedidos_recebimento": sum(c["pedidos_recebimento"] for c in clientes),
            "pedidos_expedicao": sum(c["pedidos_expedicao"] for c in clientes),
            "documentos_operacionais": sum(c["documentos_operacionais"] for c in clientes),
            "clientes_finais": sum(c["clientes_finais"] for c in clientes),
            "movimentacoes": sum(c["movimentacoes"] for c in clientes),
        },
    })


# ==========================================================
# ESTOQUE POR XML DE NF-e — 4LOG
# ==========================================================

BANCO_XML = PASTA_DB / "estoque_xml_4log.db"
MAX_XML_BYTES = 5 * 1024 * 1024  # 5 MB por arquivo
_DATA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _conectar_estoque_xml():
    """Abre/cria o banco separado para não alterar as bases operacionais existentes."""
    PASTA_DB.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BANCO_XML)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS notas_xml (
            chave TEXT PRIMARY KEY,
            numero TEXT,
            serie TEXT,
            data_emissao TEXT,
            emitente TEXT,
            emitente_cnpj TEXT NOT NULL DEFAULT '',
            destinatario TEXT,
            valor_total REAL NOT NULL DEFAULT 0,
            tipo_movimento TEXT NOT NULL CHECK(tipo_movimento IN ('entrada', 'saida')),
            nome_arquivo TEXT,
            importado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS itens_xml (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chave TEXT NOT NULL,
            codigo TEXT NOT NULL,
            descricao TEXT NOT NULL,
            ncm TEXT,
            cfop TEXT,
            unidade TEXT NOT NULL DEFAULT 'UN',
            quantidade REAL NOT NULL DEFAULT 0,
            valor_unitario REAL NOT NULL DEFAULT 0,
            valor_total REAL NOT NULL DEFAULT 0,
            FOREIGN KEY(chave) REFERENCES notas_xml(chave) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_itens_xml_produto
            ON itens_xml(codigo, unidade);
        CREATE INDEX IF NOT EXISTS idx_notas_xml_data
            ON notas_xml(data_emissao);
    """)
    # Migração compatível com o banco criado pela versão anterior.
    colunas_notas = {linha[1] for linha in conn.execute("PRAGMA table_info(notas_xml)")}
    if "emitente_cnpj" not in colunas_notas:
        conn.execute("ALTER TABLE notas_xml ADD COLUMN emitente_cnpj TEXT NOT NULL DEFAULT ''")
    conn.commit()
    return conn


def _xml_texto(elemento, caminho, padrao=""):
    encontrado = elemento.find(caminho)
    if encontrado is None or encontrado.text is None:
        return padrao
    return encontrado.text.strip()


def _xml_decimal(valor, padrao=0.0):
    try:
        # Valores fiscais do XML usam ponto decimal.
        return float(Decimal(str(valor or "0")))
    except (InvalidOperation, ValueError, TypeError):
        return padrao


def _nome_participante(participante):
    """Retorna nome/CNPJ de emitente ou destinatário, com ou sem namespace."""
    if participante is None:
        return ""

    def valor(nome):
        for elemento in list(participante):
            if elemento.tag.rsplit("}", 1)[-1] == nome:
                return (elemento.text or "").strip()
        return ""

    return valor("xNome") or valor("xFant") or valor("CNPJ") or valor("CPF")


def _documento_participante(participante):
    """Retorna CNPJ/CPF do participante para identificar o cliente com segurança."""
    if participante is None:
        return ""
    for elemento in list(participante):
        nome = elemento.tag.rsplit("}", 1)[-1]
        if nome in ("CNPJ", "CPF"):
            return "".join(c for c in (elemento.text or "") if c.isdigit())
    return ""


def _ler_nfe_xml(conteudo, nome_arquivo):
    """Lê NF-e modelo 55, inclusive dentro de nfeProc, respeitando namespace."""
    if len(conteudo) > MAX_XML_BYTES:
        raise ValueError("O arquivo ultrapassa o limite de 5 MB.")
    if b"<!DOCTYPE" in conteudo.upper() or b"<!ENTITY" in conteudo.upper():
        raise ValueError("XML com declaração de entidade/DOCTYPE não é permitido.")

    try:
        raiz = ET.fromstring(conteudo)
    except ET.ParseError as exc:
        raise ValueError(f"XML inválido: {exc}") from exc

    # Aceita tags com ou sem namespace, sem depender do namespace do fornecedor.
    def local(tag):
        return tag.rsplit("}", 1)[-1]

    def primeiro(elemento, nome):
        if elemento is None:
            return None
        if local(elemento.tag) == nome:
            return elemento
        for filho in elemento.iter():
            if local(filho.tag) == nome:
                return filho
        return None

    def filhos(elemento, nome):
        if elemento is None:
            return []
        return [e for e in elemento.iter() if local(e.tag) == nome]

    inf = primeiro(raiz, "infNFe")
    if inf is None:
        raise ValueError("O XML não contém infNFe. Envie um XML de NF-e modelo 55.")

    def texto_local(elemento, nome, padrao=""):
        filho = next((e for e in list(elemento or []) if local(e.tag) == nome), None)
        return filho.text.strip() if filho is not None and filho.text else padrao

    ide = next((e for e in list(inf) if local(e.tag) == "ide"), None)
    emit = next((e for e in list(inf) if local(e.tag) == "emit"), None)
    dest = next((e for e in list(inf) if local(e.tag) == "dest"), None)
    total = next((e for e in list(inf) if local(e.tag) == "total"), None)
    icms_tot = next((e for e in list(total or []) if local(e.tag) == "ICMSTot"), None)

    id_xml = inf.attrib.get("Id", "")
    chave = id_xml[3:] if id_xml.startswith("NFe") else ""
    if not chave:
        # Algumas fontes omitem o atributo Id; nesse caso usa a chave de protNFe.
        prot = primeiro(raiz, "chNFe")
        chave = prot.text.strip() if prot is not None and prot.text else ""
    if not chave:
        raise ValueError("Não foi possível identificar a chave de acesso da NF-e.")

    data_emissao = texto_local(ide, "dhEmi") or texto_local(ide, "dEmi")
    if len(data_emissao) >= 10:
        data_emissao = data_emissao[:10]

    itens = []
    dets = filhos(inf, "det")
    for det in dets:
        prod = next((e for e in list(det) if local(e.tag) == "prod"), None)
        if prod is None:
            continue

        def prod_text(nome, padrao=""):
            return texto_local(prod, nome, padrao)

        codigo = prod_text("cProd").strip()
        descricao = prod_text("xProd", "Produto sem descrição").strip()
        unidade = prod_text("uCom", "UN").strip().upper() or "UN"
        quantidade = _xml_decimal(prod_text("qCom"))
        valor_total = _xml_decimal(prod_text("vProd"))
        valor_unitario = _xml_decimal(prod_text("vUnCom"))
        if not valor_unitario and quantidade:
            valor_unitario = valor_total / quantidade

        itens.append({
            "codigo": codigo or descricao[:80],
            "descricao": descricao,
            "ncm": prod_text("NCM"),
            "cfop": prod_text("CFOP"),
            "unidade": unidade,
            "quantidade": quantidade,
            "valor_unitario": valor_unitario,
            "valor_total": valor_total,
        })

    if not itens:
        raise ValueError("Nenhum item de produto foi encontrado nessa NF-e.")

    return {
        "chave": chave,
        "numero": texto_local(ide, "nNF"),
        "serie": texto_local(ide, "serie"),
        "data_emissao": data_emissao,
        "emitente": _nome_participante(emit),
        "emitente_cnpj": _documento_participante(emit),
        "destinatario": _nome_participante(dest),
        "valor_total": _xml_decimal(texto_local(icms_tot, "vNF")),
        "nome_arquivo": Path(nome_arquivo or "nota.xml").name[:255],
        "itens": itens,
    }


def _validar_csrf_xml():
    enviado = request.headers.get("X-CSRF-Token", "")
    esperado = session.get("csrf", "")
    return bool(enviado and esperado and secrets.compare_digest(enviado, esperado))


@api_4log.route("/api/4log/estoque-xml/importar", methods=["POST"])
def importar_xml_estoque():
    """Importa uma ou várias NF-e e grava seus itens como entrada ou saída."""
    if not _validar_csrf_xml():
        return jsonify(erro="Sessão de segurança expirada. Atualize a página e tente novamente."), 400

    tipo_movimento = request.form.get("tipo_movimento", "").strip().lower()
    if tipo_movimento not in ("entrada", "saida"):
        return jsonify(erro="Selecione se os XMLs representam entrada ou saída de estoque."), 400

    arquivos = request.files.getlist("arquivos")
    arquivos = [arquivo for arquivo in arquivos if arquivo and arquivo.filename]
    if not arquivos:
        return jsonify(erro="Selecione pelo menos um arquivo XML."), 400

    importados = []
    duplicados = []
    erros = []
    conn = _conectar_estoque_xml()
    try:
        for arquivo in arquivos:
            nome = Path(arquivo.filename).name
            if not nome.lower().endswith(".xml"):
                erros.append({"arquivo": nome, "erro": "O arquivo não tem extensão .xml."})
                continue

            conteudo = arquivo.read(MAX_XML_BYTES + 1)
            try:
                nota = _ler_nfe_xml(conteudo, nome)
            except ValueError as exc:
                erros.append({"arquivo": nome, "erro": str(exc)})
                continue

            existente = conn.execute(
                "SELECT tipo_movimento FROM notas_xml WHERE chave = ?",
                (nota["chave"],),
            ).fetchone()
            if existente:
                duplicados.append({
                    "arquivo": nome,
                    "numero": nota["numero"],
                    "tipo_existente": existente["tipo_movimento"],
                })
                continue

            try:
                conn.execute("""
                    INSERT INTO notas_xml
                    (chave, numero, serie, data_emissao, emitente, emitente_cnpj,
                     destinatario, valor_total, tipo_movimento, nome_arquivo, importado_em)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    nota["chave"], nota["numero"], nota["serie"],
                    nota["data_emissao"], nota["emitente"], nota["emitente_cnpj"],
                    nota["destinatario"], nota["valor_total"], tipo_movimento, nota["nome_arquivo"],
                    datetime.now().isoformat(timespec="seconds"),
                ))
                conn.executemany("""
                    INSERT INTO itens_xml
                    (chave, codigo, descricao, ncm, cfop, unidade,
                     quantidade, valor_unitario, valor_total)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [
                    (
                        nota["chave"], item["codigo"], item["descricao"],
                        item["ncm"], item["cfop"], item["unidade"],
                        item["quantidade"], item["valor_unitario"], item["valor_total"],
                    )
                    for item in nota["itens"]
                ])
                conn.commit()
                importados.append({
                    "arquivo": nome,
                    "numero": nota["numero"],
                    "itens": len(nota["itens"]),
                    "valor_total": nota["valor_total"],
                })
            except sqlite3.Error:
                conn.rollback()
                erros.append({"arquivo": nome, "erro": "Não foi possível gravar os dados dessa nota."})

        return jsonify({
            "mensagem": f"{len(importados)} nota(s) importada(s).",
            "importados": importados,
            "duplicados": duplicados,
            "erros": erros,
        })
    finally:
        conn.close()


@api_4log.route("/api/4log/estoque-xml/resumo", methods=["GET"])
def resumo_estoque_xml():
    """Retorna ranking e detalhe do estoque agrupados pelo emitente da NF-e."""
    conn = _conectar_estoque_xml()
    try:
        notas_total = conn.execute("SELECT COUNT(*) FROM notas_xml").fetchone()[0]
        valor_notas = conn.execute(
            "SELECT COALESCE(SUM(valor_total), 0) FROM notas_xml"
        ).fetchone()[0]

        movimentos = conn.execute("""
            SELECT i.codigo, i.descricao, i.ncm, i.unidade,
                   n.data_emissao, n.tipo_movimento, n.chave,
                   n.emitente, COALESCE(n.emitente_cnpj, '') AS emitente_cnpj,
                   i.quantidade, i.valor_unitario, i.valor_total
            FROM itens_xml i
            JOIN notas_xml n ON n.chave = i.chave
            ORDER BY COALESCE(n.data_emissao, ''), n.importado_em, i.id
        """).fetchall()

        # Agrupa por CNPJ/CPF do emitente; quando não existe documento, usa o nome.
        clientes = {}
        for linha in movimentos:
            nome = (linha["emitente"] or "Emitente não identificado").strip()
            documento = "".join(c for c in (linha["emitente_cnpj"] or "") if c.isdigit())
            chave_cliente = ("doc:" + documento) if documento else ("nome:" + " ".join(nome.casefold().split()))
            cliente = clientes.setdefault(chave_cliente, {
                "chave": chave_cliente,
                "nome": nome,
                "cnpj": documento,
                "produtos_map": {},
                "notas": set(),
                "valor_notas": 0.0,
            })
            if cliente["nome"] == "Emitente não identificado" and nome != cliente["nome"]:
                cliente["nome"] = nome
            cliente["notas"].add(linha["chave"])
            produto_chave = (linha["codigo"], linha["unidade"])
            produto = cliente["produtos_map"].setdefault(produto_chave, {
                "codigo": linha["codigo"], "descricao": linha["descricao"],
                "ncm": linha["ncm"] or "", "unidade": linha["unidade"] or "UN",
                "qtd_entrada": 0.0, "qtd_saida": 0.0, "valor_entrada": 0.0,
                "notas": set(),
            })
            if len(linha["descricao"] or "") > len(produto["descricao"] or ""):
                produto["descricao"] = linha["descricao"]
            produto["notas"].add(linha["chave"])
            qtd = float(linha["quantidade"] or 0)
            total = float(linha["valor_total"] or 0)
            if linha["tipo_movimento"] == "entrada":
                produto["qtd_entrada"] += qtd
                produto["valor_entrada"] += total
            else:
                produto["qtd_saida"] += qtd

        # Pico histórico do valor de estoque (consolidado por SKU), medido no fim de cada dia.
        sku_estado = {}
        valor_corrente = 0.0
        pico_valor, pico_data, dia_atual = 0.0, "", None
        for linha in movimentos:
            dia = (linha["data_emissao"] or "")[:10]
            if dia_atual is not None and dia != dia_atual and valor_corrente > pico_valor:
                pico_valor, pico_data = valor_corrente, dia_atual
            dia_atual = dia
            est = sku_estado.setdefault((linha["codigo"], linha["unidade"]), {
                "ent": 0.0, "sai": 0.0, "val_ent": 0.0, "valor": 0.0,
            })
            qtd = float(linha["quantidade"] or 0)
            if linha["tipo_movimento"] == "entrada":
                est["ent"] += qtd
                est["val_ent"] += float(linha["valor_total"] or 0)
            else:
                est["sai"] += qtd
            custo = est["val_ent"] / est["ent"] if est["ent"] > 0 else 0.0
            novo_valor = max(est["ent"] - est["sai"], 0) * custo
            valor_corrente += novo_valor - est["valor"]
            est["valor"] = novo_valor
        if dia_atual is not None and valor_corrente > pico_valor:
            pico_valor, pico_data = valor_corrente, dia_atual

        lista_clientes = []
        todos_produtos = {}
        valor_estoque_geral = 0.0
        produtos_negativos_geral = 0
        for cliente in clientes.values():
            lista_produtos = []
            valor_cliente = 0.0
            negativos_cliente = 0
            for produto in cliente["produtos_map"].values():
                saldo = produto["qtd_entrada"] - produto["qtd_saida"]
                custo_medio = produto["valor_entrada"] / produto["qtd_entrada"] if produto["qtd_entrada"] > 0 else 0.0
                valor_estimado = max(saldo, 0) * custo_medio
                if saldo < 0:
                    negativos_cliente += 1
                valor_cliente += valor_estimado
                lista_produtos.append({
                    "codigo": produto["codigo"], "descricao": produto["descricao"],
                    "ncm": produto["ncm"], "unidade": produto["unidade"],
                    "qtd_entrada": round(produto["qtd_entrada"], 4),
                    "qtd_saida": round(produto["qtd_saida"], 4),
                    "saldo": round(saldo, 4), "custo_medio": round(custo_medio, 4),
                    "valor_estimado": round(valor_estimado, 2),
                    "notas": len(produto["notas"]),
                })
                # Agregação global por código/unidade para manter a tabela geral.
                pk = (produto["codigo"], produto["unidade"])
                global_p = todos_produtos.setdefault(pk, {
                    "codigo": produto["codigo"], "descricao": produto["descricao"],
                    "ncm": produto["ncm"] or "", "unidade": produto["unidade"] or "UN",
                    "qtd_entrada": 0.0, "qtd_saida": 0.0, "valor_entrada": 0.0,
                    "notas": set(),
                })
                global_p["qtd_entrada"] += produto["qtd_entrada"]
                global_p["qtd_saida"] += produto["qtd_saida"]
                global_p["valor_entrada"] += produto["valor_entrada"]
                global_p["notas"].update(produto["notas"])
            lista_produtos.sort(key=lambda p: (p["descricao"] or "").casefold())
            lista_clientes.append({
                "chave": cliente["chave"], "nome": cliente["nome"], "cnpj": cliente["cnpj"],
                "notas_importadas": len(cliente["notas"]),
                "produtos": len(lista_produtos),
                "produtos_negativos": negativos_cliente,
                "valor_estoque_estimado": round(valor_cliente, 2),
                "produtos_detalhe": lista_produtos,
            })
            valor_estoque_geral += valor_cliente
            produtos_negativos_geral += negativos_cliente

        lista_clientes.sort(key=lambda c: c["valor_estoque_estimado"], reverse=True)
        lista_global = []
        for p in todos_produtos.values():
            saldo = p["qtd_entrada"] - p["qtd_saida"]
            custo = p["valor_entrada"] / p["qtd_entrada"] if p["qtd_entrada"] > 0 else 0.0
            valor = max(saldo, 0) * custo
            lista_global.append({
                "codigo": p["codigo"], "descricao": p["descricao"], "ncm": p["ncm"],
                "unidade": p["unidade"], "qtd_entrada": round(p["qtd_entrada"], 4),
                "qtd_saida": round(p["qtd_saida"], 4), "saldo": round(saldo, 4),
                "custo_medio": round(custo, 4), "valor_estimado": round(valor, 2),
                "notas": len(p["notas"]),
            })
        lista_global.sort(key=lambda p: (p["descricao"] or "").casefold())
        # Valor e saldos negativos gerais usam o estoque consolidado por SKU (todos os CNPJs),
        # para um CNPJ com saldo negativo abater o saldo positivo do outro.
        valor_estoque_geral = sum(p["valor_estimado"] for p in lista_global)
        produtos_negativos_geral = sum(1 for p in lista_global if p["saldo"] < 0)

        # Filtros opcionais da lista de notas (tipo, busca, período e limite).
        filtros_notas = ["1=1"]
        params_notas = []
        tipo_filtro = request.args.get("tipo", "").strip().lower()
        if tipo_filtro in ("entrada", "saida"):
            filtros_notas.append("tipo_movimento = ?")
            params_notas.append(tipo_filtro)
        busca = request.args.get("busca", "").strip()[:80]
        if busca:
            like = "%" + busca.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            filtros_notas.append(
                "(numero LIKE ? ESCAPE '\\' OR emitente LIKE ? ESCAPE '\\' "
                "OR destinatario LIKE ? ESCAPE '\\' OR chave LIKE ? ESCAPE '\\' "
                "OR emitente_cnpj LIKE ? ESCAPE '\\')"
            )
            params_notas.extend([like] * 5)
        for param, operador in (("data_ini", ">="), ("data_fim", "<=")):
            valor_data = request.args.get(param, "").strip()
            if _DATA_ISO.match(valor_data):
                filtros_notas.append(f"substr(data_emissao, 1, 10) {operador} ?")
                params_notas.append(valor_data)
        try:
            limite_notas = min(max(int(request.args.get("limite", 30)), 1), 500)
        except ValueError:
            limite_notas = 30
        where_notas = " AND ".join(filtros_notas)

        notas_filtradas = conn.execute(
            f"SELECT COUNT(*) FROM notas_xml WHERE {where_notas}", params_notas
        ).fetchone()[0]
        notas = conn.execute(f"""
            SELECT chave, numero, serie, data_emissao, emitente,
                   COALESCE(emitente_cnpj, '') AS emitente_cnpj, destinatario,
                   valor_total, tipo_movimento, nome_arquivo, importado_em,
                   (SELECT COUNT(*) FROM itens_xml i WHERE i.chave = notas_xml.chave) AS qtd_itens
            FROM notas_xml
            WHERE {where_notas}
            ORDER BY COALESCE(data_emissao, '') DESC, importado_em DESC
            LIMIT {limite_notas}
        """, params_notas).fetchall()

        # Totais por tipo de movimento (considera todas as notas importadas).
        por_tipo = {"entrada": (0, 0.0), "saida": (0, 0.0)}
        for linha in conn.execute("""
            SELECT tipo_movimento, COUNT(*), COALESCE(SUM(valor_total), 0)
            FROM notas_xml GROUP BY tipo_movimento
        """):
            por_tipo[linha[0]] = (linha[1], float(linha[2] or 0))

        # Série mensal (últimos 12 meses com notas) de valor fiscal por movimento.
        mensal_map = {}
        for linha in conn.execute("""
            SELECT substr(data_emissao, 1, 7) AS mes, tipo_movimento,
                   COUNT(*), COALESCE(SUM(valor_total), 0)
            FROM notas_xml
            WHERE length(COALESCE(data_emissao, '')) >= 7
            GROUP BY mes, tipo_movimento
        """):
            item = mensal_map.setdefault(linha[0], {
                "mes": linha[0], "entrada": 0.0, "saida": 0.0,
                "notas_entrada": 0, "notas_saida": 0,
            })
            item[linha[1]] = round(float(linha[3] or 0), 2)
            item["notas_" + linha[1]] = linha[2]
        mensal = [mensal_map[m] for m in sorted(mensal_map)][-12:]

        top_produtos = sorted(
            (p for p in lista_global if p["valor_estimado"] > 0),
            key=lambda p: p["valor_estimado"], reverse=True,
        )[:8]

        return jsonify({
            "resumo": {
                "notas_importadas": notas_total,
                "valor_total_notas": round(float(valor_notas or 0), 2),
                "produtos": len(lista_global),
                "produtos_negativos": produtos_negativos_geral,
                "valor_estoque_estimado": round(valor_estoque_geral, 2),
                "valor_pico_estoque": round(pico_valor, 2),
                "data_pico_estoque": pico_data,
                "clientes_com_estoque": sum(1 for c in lista_clientes if c["valor_estoque_estimado"] > 0),
                "notas_entrada": por_tipo["entrada"][0],
                "notas_saida": por_tipo["saida"][0],
                "valor_entradas": round(por_tipo["entrada"][1], 2),
                "valor_saidas": round(por_tipo["saida"][1], 2),
            },
            "clientes": lista_clientes,
            "produtos": lista_global,
            "mensal": mensal,
            "top_produtos": top_produtos,
            "notas": [dict(n) for n in notas],
            "notas_filtradas": notas_filtradas,
            "observacao": (
                "O ranking identifica cada cliente pelo CNPJ/CPF do emitente da NF-e; "
                "se o documento não estiver disponível, usa o nome do emitente. O estoque "
                "considera somente XMLs importados e o valor é estimado pelo custo médio "
                "das entradas. Importe o saldo inicial e todas as movimentações. Como o "
                "agrupamento é pelo emitente, confirme que esse critério corresponde ao "
                "seu processo operacional, principalmente nas notas de saída."
            ),
        })
    finally:
        conn.close()

@api_4log.route("/api/4log/estoque-xml/notas/<chave>", methods=["DELETE"])
def excluir_nota_xml(chave):
    """Exclui uma nota importada para permitir corrigir o tipo de movimento."""
    if not _validar_csrf_xml():
        return jsonify(erro="Sessão de segurança expirada. Atualize a página e tente novamente."), 400

    conn = _conectar_estoque_xml()
    try:
        cursor = conn.execute("DELETE FROM notas_xml WHERE chave = ?", (chave,))
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify(erro="Nota não encontrada."), 404
        return jsonify(mensagem="Nota removida do cálculo do estoque.")
    finally:
        conn.close()


@api_4log.route("/api/4log/estoque-xml/notas/<chave>", methods=["GET"])
def detalhe_nota_xml(chave):
    """Retorna o cabeçalho e os itens de uma nota importada."""
    conn = _conectar_estoque_xml()
    try:
        nota = conn.execute("""
            SELECT chave, numero, serie, data_emissao, emitente,
                   COALESCE(emitente_cnpj, '') AS emitente_cnpj, destinatario,
                   valor_total, tipo_movimento, nome_arquivo, importado_em
            FROM notas_xml WHERE chave = ?
        """, (chave,)).fetchone()
        if nota is None:
            return jsonify(erro="Nota não encontrada."), 404
        itens = conn.execute("""
            SELECT codigo, descricao, ncm, cfop, unidade,
                   quantidade, valor_unitario, valor_total
            FROM itens_xml WHERE chave = ? ORDER BY id
        """, (chave,)).fetchall()
        return jsonify(nota=dict(nota), itens=[dict(i) for i in itens])
    finally:
        conn.close()


@api_4log.route("/api/4log/estoque-xml/notas/<chave>/tipo", methods=["POST"])
def alterar_tipo_nota_xml(chave):
    """Corrige o tipo de movimento de uma nota sem precisar reimportar o XML."""
    if not _validar_csrf_xml():
        return jsonify(erro="Sessão de segurança expirada. Atualize a página e tente novamente."), 400

    corpo = request.get_json(silent=True) or {}
    tipo = str(corpo.get("tipo_movimento", "")).strip().lower()
    if tipo not in ("entrada", "saida"):
        return jsonify(erro="Tipo de movimento inválido."), 400

    conn = _conectar_estoque_xml()
    try:
        cursor = conn.execute(
            "UPDATE notas_xml SET tipo_movimento = ? WHERE chave = ?", (tipo, chave)
        )
        conn.commit()
        if cursor.rowcount == 0:
            return jsonify(erro="Nota não encontrada."), 404
        return jsonify(mensagem="Tipo de movimento atualizado.", tipo_movimento=tipo)
    finally:
        conn.close()


def _celula_csv(valor):
    """Formata para CSV pt-BR e neutraliza fórmulas vindas de textos do XML."""
    if isinstance(valor, bool) or valor is None:
        return ""
    if isinstance(valor, (int, float)):
        return str(round(valor, 4)).replace(".", ",")
    texto = str(valor)
    if texto[:1] in ("=", "+", "-", "@", "\t", "\r"):
        texto = "'" + texto
    return texto


@api_4log.route("/api/4log/estoque-xml/exportar", methods=["GET"])
def exportar_xml_estoque():
    """
    Exporta CSV (separador ';', abre direto no Excel).
    conteudo=estoque  -> produtos (use cliente=<chave> para um só cliente)
    conteudo=notas    -> notas importadas, respeitando os filtros de /resumo
    """
    conteudo = request.args.get("conteudo", "estoque")
    dados = resumo_estoque_xml().get_json()
    buffer = io.StringIO()
    escritor = csv.writer(buffer, delimiter=";")

    if conteudo == "notas":
        escritor.writerow([
            "Data", "Número", "Série", "Movimento", "Emitente", "CNPJ/CPF emitente",
            "Destinatário", "Itens", "Valor da NF-e", "Chave de acesso", "Arquivo",
        ])
        for n in dados["notas"]:
            escritor.writerow([_celula_csv(v) for v in (
                n["data_emissao"], n["numero"], n["serie"],
                "Entrada" if n["tipo_movimento"] == "entrada" else "Saída",
                n["emitente"], n["emitente_cnpj"], n["destinatario"],
                n["qtd_itens"], n["valor_total"], n["chave"], n["nome_arquivo"],
            )])
        nome_base = "notas_xml_4log"
    else:
        cliente = request.args.get("cliente", "").strip()
        escritor.writerow([
            "Cliente", "CNPJ/CPF", "Código", "Produto", "NCM", "Unidade",
            "Entradas", "Saídas", "Saldo", "Custo médio", "Valor estimado",
        ])
        for c in dados["clientes"]:
            if cliente and c["chave"] != cliente:
                continue
            for p in c["produtos_detalhe"]:
                escritor.writerow([_celula_csv(v) for v in (
                    c["nome"], c["cnpj"], p["codigo"], p["descricao"], p["ncm"],
                    p["unidade"], p["qtd_entrada"], p["qtd_saida"], p["saldo"],
                    p["custo_medio"], p["valor_estimado"],
                )])
        nome_base = "estoque_xml_4log"

    nome = f"{nome_base}_{date.today().isoformat()}.csv"
    return Response(
        "\ufeff" + buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{nome}"'},
    )