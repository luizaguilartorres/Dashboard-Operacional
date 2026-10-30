from flask import Flask, jsonify, request, Blueprint, session
import io
import re
import unicodedata
import sqlite3
import unicodedata
from datetime import datetime
from pathlib import Path

CAMINHO = Path(__file__).parent
(CAMINHO / "db").mkdir(exist_ok=True)
CAMINHO_BANCO = CAMINHO / "db" / "dados_base_sebastian.db"

sebastian = Blueprint("sebastian", __name__)


@sebastian.before_request
def proteger_api():
    if "usuario" not in session:
        return jsonify(erro="não autenticado"), 401
    if session.get("empresa") not in ("4log", "sebastian"):
        return jsonify(erro="sem permissão"), 403


def data():
    ano = request.args.get("ano")  # Ex.: /api/sebastian/recebimento/resumo?ano=2026&mes=9 -> ano="2026"
    mes = request.args.get("mes")
    if mes:
        mes = mes.zfill(2)  # completa com zero à esquerda. Ex: 9 -> 09
    return ano, mes


def filtro_dados(coluna="data"):
    ano, mes = data()
    filtro, params = [], []
    if ano:
        filtro.append(f"strftime('%Y',{coluna}) = ?")
        params.append(ano)
    if mes:
        filtro.append(f"strftime('%m',{coluna}) = ?")
        params.append(mes)
    where = "WHERE " + " AND ".join(filtro) if filtro else ""
    return where, params


def consultar(sql, params):
    conn = sqlite3.connect(CAMINHO_BANCO)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(l) for l in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


DIAS = """CASE CAST(strftime('%w', data) AS INTEGER)
            WHEN 0 THEN 'Domingo'
            WHEN 1 THEN 'Segunda'
            WHEN 2 THEN 'Terça'
            WHEN 3 THEN 'Quarta'
            WHEN 4 THEN 'Quinta'
            WHEN 5 THEN 'Sexta'
            WHEN 6 THEN 'Sábado'
            END"""

# --------------------------------------------------------------------------
# Como o "carro" (viagem) é contado. Mude aqui se a regra do cliente mudar.
#  - Recebimento: a coluna "recebimento" é o número da viagem (um número por carro).
#    Agora ele é GERADO AUTOMATICAMENTE nos lançamentos (veja _numerar_recebimentos).
#  - Expedição: o mesmo romaneio pode ter vários carros (ex.: vários Fiorinos
#    saindo no mesmo romaneio), então um carro = romaneio + placa.
# --------------------------------------------------------------------------
CARRO_REC = "recebimento"
CARRO_EXP = "romaneio"


def rota(sql, coluna="data"):
    # No SQL use {where} para o filtro de ano/mês, {dias} para o nome do dia da semana,
    # {car_rec} para o carro do recebimento e {car_exp} para o carro da expedição.
    # Não coloque "f" antes das aspas: o .format() abaixo é quem preenche.
    where, params = filtro_dados(coluna)
    return jsonify(consultar(
        sql.format(where=where, dias=DIAS, car_rec=CARRO_REC, car_exp=CARRO_EXP), params))


# ==========================
# API RECEBIMENTO-VEICULOS
# ==========================
@sebastian.route("/api/sebastian/recebimento/carro/por-hora")
def sebastian_recebimento_carro_por_hora():
    return rota("""
        SELECT strftime('%H', hora) AS hora, COUNT(DISTINCT {car_rec}) AS total
        FROM recebimento
        {where}
        GROUP BY strftime('%H', hora)
        ORDER BY hora
    """)


@sebastian.route("/api/sebastian/recebimento/carro/data")
def sebastian_recebimento_carro_por_data():
    return rota("""
        SELECT date(data) AS data, COUNT(DISTINCT {car_rec}) AS total
        FROM recebimento
        {where}
        GROUP BY date(data)
        ORDER BY data
    """)


@sebastian.route("/api/sebastian/recebimento/carro/dias-semana")
def sebastian_recebimento_carro_por_dias_semana():
    return rota("""
        SELECT strftime('%w', data) AS num_dia,
               {dias} AS dia_semana,
               COUNT(DISTINCT {car_rec}) AS total
        FROM recebimento
        {where}
        GROUP BY strftime('%w', data)
        ORDER BY num_dia
    """)


@sebastian.route("/api/sebastian/recebimento/carro/total")
def sebastian_recebimento_veiculos_recebidos():
    return rota("""
        SELECT COUNT(DISTINCT {car_rec}) AS total
        FROM recebimento
        {where}
    """)


@sebastian.route("/api/sebastian/recebimento/carro/por-tipo")
def sebastian_recebimento_tipos_de_veiculos():
    return rota("""
        SELECT veiculo, COUNT(DISTINCT {car_rec}) AS total
        FROM recebimento
        {where}
        GROUP BY veiculo
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/recebimento/carro/por-cliente")
def sebastian_recebimento_carro_por_cliente():
    return rota("""
        SELECT cliente, COUNT(DISTINCT {car_rec}) AS total
        FROM recebimento
        {where}
        GROUP BY cliente
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/recebimento/carro/por-mes")
def sebastian_recebimento_carro_por_mes():
    return rota("""
        SELECT strftime('%Y-%m', data) AS mes, COUNT(DISTINCT {car_rec}) AS total
        FROM recebimento
        {where}
        GROUP BY strftime('%Y-%m', data)
        ORDER BY mes
    """)


# ==========================
# API RECEBIMENTO-PALLETS
# ==========================
@sebastian.route("/api/sebastian/recebimento/pallet/total")
def sebastian_recebimento_total_pallet():
    return rota("""
        SELECT COALESCE(SUM(qtd_pallet), 0) AS total
        FROM recebimento
        {where}
    """)


@sebastian.route("/api/sebastian/recebimento/pallet/veiculo")
def sebastian_recebimento_pallet_veiculo():
    return rota("""
        SELECT veiculo, COALESCE(SUM(qtd_pallet), 0) AS total
        FROM recebimento
        {where}
        GROUP BY veiculo
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/recebimento/pallet/media-por-veiculo")
def sebastian_recebimento_pallet_media_veiculo():
    # Média de pallets por carro (viagem), não por linha da tabela.
    # "total" aqui é a média, para manter o mesmo nome de campo das outras rotas.
    return rota("""
        SELECT veiculo,
               ROUND(COALESCE(SUM(qtd_pallet), 0) * 1.0
                     / NULLIF(COUNT(DISTINCT {car_rec}), 0), 1) AS total
        FROM recebimento
        {where}
        GROUP BY veiculo
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/recebimento/pallet/cliente")
def sebastian_recebimento_pallet_cliente():
    return rota("""
        SELECT cliente, COALESCE(SUM(qtd_pallet), 0) AS total
        FROM recebimento
        {where}
        GROUP BY cliente
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/recebimento/pallet/data")
def sebastian_recebimento_pallet_data():
    return rota("""
        SELECT date(data) AS data, COALESCE(SUM(qtd_pallet), 0) AS total
        FROM recebimento
        {where}
        GROUP BY date(data)
        ORDER BY data
    """)


@sebastian.route("/api/sebastian/recebimento/pallet/dia-semana")
def sebastian_recebimento_pallet_por_dia_semana():
    return rota("""
        SELECT strftime('%w', data) AS num_dia,
               {dias} AS dia_semana,
               COALESCE(SUM(qtd_pallet), 0) AS total
        FROM recebimento
        {where}
        GROUP BY strftime('%w', data)
        ORDER BY num_dia
    """)


@sebastian.route("/api/sebastian/recebimento/pallet/por-mes")
def sebastian_recebimento_pallet_por_mes():
    return rota("""
        SELECT strftime('%Y-%m', data) AS mes,
               COALESCE(SUM(qtd_pallet), 0) AS total
        FROM recebimento
        {where}
        GROUP BY strftime('%Y-%m', data)
        ORDER BY mes
    """)


@sebastian.route("/api/sebastian/recebimento/pallet/por-hora")
def sebastian_recebimento_pallet_por_hora():
    return rota("""
        SELECT strftime('%H', hora) AS hora,
               COALESCE(SUM(qtd_pallet), 0) AS total
        FROM recebimento
        {where}
        GROUP BY strftime('%H', hora)
        ORDER BY hora
    """)


# =============================
# API RECEBIMENTO-NOTAS
# (a Sebastian tem nota de transferência e nota de depósito; não tem pedido)
# =============================
@sebastian.route("/api/sebastian/recebimento/nota-transferencia/total")
def sebastian_recebimento_total_notas_transferencia():
    return rota("""
        SELECT COUNT(DISTINCT nota_de_transferencia) AS total
        FROM recebimento
        {where}
    """)


@sebastian.route("/api/sebastian/recebimento/nota-transferencia/data")
def sebastian_recebimento_notas_transferencia_data():
    return rota("""
        SELECT date(data) AS data, COUNT(DISTINCT nota_de_transferencia) AS total
        FROM recebimento
        {where}
        GROUP BY date(data)
        ORDER BY data
    """)


@sebastian.route("/api/sebastian/recebimento/nota-deposito/total")
def sebastian_recebimento_total_notas_deposito():
    return rota("""
        SELECT COUNT(DISTINCT nota_de_deposito) AS total
        FROM recebimento
        {where}
    """)


@sebastian.route("/api/sebastian/recebimento/nota-deposito/data")
def sebastian_recebimento_notas_deposito_data():
    return rota("""
        SELECT date(data) AS data, COUNT(DISTINCT nota_de_deposito) AS total
        FROM recebimento
        {where}
        GROUP BY date(data)
        ORDER BY data
    """)


@sebastian.route("/api/sebastian/recebimento/resumo")
def sebastian_recebimento_resumo():
    # Cards do topo: tudo em uma única chamada
    return rota("""
        SELECT COUNT(DISTINCT {car_rec})                AS carros,
               COALESCE(SUM(qtd_pallet), 0)             AS pallets,
               COUNT(DISTINCT nota_de_transferencia)    AS notas_transferencia,
               COUNT(DISTINCT nota_de_deposito)         AS notas_deposito,
               ROUND(COALESCE(SUM(qtd_pallet), 0) * 1.0
                     / NULLIF(COUNT(DISTINCT {car_rec}), 0), 1) AS media_pallet_carro
        FROM recebimento
        {where}
    """)


# ==========================
# API EXPEDIÇÃO-VEICULOS
# ==========================
@sebastian.route("/api/sebastian/expedicao/carro/total")
def sebastian_expedicao_carro_total():
    return rota("""
        SELECT veiculo, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY veiculo
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/carro/quantidade")
def sebastian_expedicao_carro_quantidade():
    return rota("""
        SELECT COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
    """)


@sebastian.route("/api/sebastian/expedicao/carro/veiculo")
def sebastian_expedicao_carro_modelo():
    return rota("""
        SELECT veiculo, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY veiculo
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/carro/por-cliente")
def sebastian_expedicao_carro_por_cliente():
    return rota("""
        SELECT cliente, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY cliente
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/carro/data")
def sebastian_expedicao_carro_por_data():
    return rota("""
        SELECT date(data) AS data, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY date(data)
        ORDER BY data
    """)


@sebastian.route("/api/sebastian/expedicao/carro/por-hora")
def sebastian_expedicao_carro_por_hora():
    return rota("""
        SELECT strftime('%H', hora) AS hora, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY strftime('%H', hora)
        ORDER BY hora
    """)


@sebastian.route("/api/sebastian/expedicao/carro/dias-semana")
def sebastian_expedicao_carro_por_dias_semana():
    return rota("""
        SELECT strftime('%w', data) AS num_dia,
               {dias} AS dia_semana,
               COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY strftime('%w', data)
        ORDER BY num_dia
    """)


@sebastian.route("/api/sebastian/expedicao/carro/por-transportadora")
def sebastian_expedicao_carro_por_transportadora():
    return rota("""
        SELECT transportadora, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY transportadora
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/carro/por-motorista")
def sebastian_expedicao_carro_por_motorista():
    return rota("""
        SELECT motorista, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY motorista
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/carro/por-destino")
def sebastian_expedicao_carro_por_destino():
    return rota("""
        SELECT destino, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY destino
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/carro/por-mes")
def sebastian_expedicao_carro_por_mes():
    return rota("""
        SELECT strftime('%Y-%m', data) AS mes, COUNT(DISTINCT {car_exp}) AS total
        FROM expedicao
        {where}
        GROUP BY strftime('%Y-%m', data)
        ORDER BY mes
    """)


# ==========================
# API EXPEDIÇÃO-PALLETS
# ==========================
@sebastian.route("/api/sebastian/expedicao/pallet/total")
def sebastian_expedicao_pallet_total():
    return rota("""
        SELECT COALESCE(SUM(qtd_pallet), 0) AS total
        FROM expedicao
        {where}
    """)


@sebastian.route("/api/sebastian/expedicao/pallet/por-cliente")
def sebastian_expedicao_pallet_por_cliente():
    return rota("""
        SELECT cliente, COALESCE(SUM(qtd_pallet), 0) AS total
        FROM expedicao
        {where}
        GROUP BY cliente
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/pallet/por-destino")
def sebastian_expedicao_pallet_por_destino():
    return rota("""
        SELECT destino, COALESCE(SUM(qtd_pallet), 0) AS total
        FROM expedicao
        {where}
        GROUP BY destino
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/pallet/data")
def sebastian_expedicao_pallet_por_data():
    return rota("""
        SELECT date(data) AS data, COALESCE(SUM(qtd_pallet), 0) AS total
        FROM expedicao
        {where}
        GROUP BY date(data)
        ORDER BY data
    """)


@sebastian.route("/api/sebastian/expedicao/pallet/veiculo")
def sebastian_expedicao_pallet_veiculo():
    return rota("""
        SELECT veiculo, COALESCE(SUM(qtd_pallet), 0) AS total
        FROM expedicao
        {where}
        GROUP BY veiculo
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/pallet/media-por-veiculo")
def sebastian_expedicao_pallet_media_veiculo():
    # Média de pallets por carro, não por linha da tabela
    return rota("""
        SELECT veiculo,
               ROUND(COALESCE(SUM(qtd_pallet), 0) * 1.0
                     / NULLIF(COUNT(DISTINCT {car_exp}), 0), 1) AS total
        FROM expedicao
        {where}
        GROUP BY veiculo
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/pallet/por-transportadora")
def sebastian_expedicao_pallet_por_transportadora():
    return rota("""
        SELECT transportadora, COALESCE(SUM(qtd_pallet), 0) AS total
        FROM expedicao
        {where}
        GROUP BY transportadora
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/pallet/dia-semana")
def sebastian_expedicao_pallet_por_dia_semana():
    return rota("""
        SELECT strftime('%w', data) AS num_dia,
               {dias} AS dia_semana,
               COALESCE(SUM(qtd_pallet), 0) AS total
        FROM expedicao
        {where}
        GROUP BY strftime('%w', data)
        ORDER BY num_dia
    """)


@sebastian.route("/api/sebastian/expedicao/pallet/por-mes")
def sebastian_expedicao_pallet_por_mes():
    return rota("""
        SELECT strftime('%Y-%m', data) AS mes,
               COALESCE(SUM(qtd_pallet), 0) AS total
        FROM expedicao
        {where}
        GROUP BY strftime('%Y-%m', data)
        ORDER BY mes
    """)


# ==========================
# API EXPEDIÇÃO-PEDIDOS E NOTAS
# ==========================
@sebastian.route("/api/sebastian/expedicao/pedido/total")
def sebastian_expedicao_pedido_total():
    return rota("""
        SELECT COUNT(DISTINCT pedido) AS total
        FROM expedicao
        {where}
    """)


@sebastian.route("/api/sebastian/expedicao/pedido/cliente")
def sebastian_expedicao_pedido_por_cliente():
    return rota("""
        SELECT cliente, COUNT(DISTINCT pedido) AS total
        FROM expedicao
        {where}
        GROUP BY cliente
        ORDER BY total DESC
    """)


@sebastian.route("/api/sebastian/expedicao/nota-fiscal/total")
def sebastian_expedicao_nota_total():
    return rota("""
        SELECT COUNT(DISTINCT nota_fiscal) AS total
        FROM expedicao
        {where}
    """)


@sebastian.route("/api/sebastian/expedicao/nota-fiscal/data")
def sebastian_expedicao_nota_data():
    return rota("""
        SELECT date(data) AS data, COUNT(DISTINCT nota_fiscal) AS total
        FROM expedicao
        {where}
        GROUP BY date(data)
        ORDER BY data
    """)


@sebastian.route("/api/sebastian/expedicao/resumo")
def sebastian_expedicao_resumo():
    # Cards do topo: tudo em uma única chamada
    return rota("""
        SELECT COUNT(DISTINCT {car_exp})                AS carros,
               COALESCE(SUM(qtd_pallet), 0)             AS pallets,
               COUNT(DISTINCT pedido)                   AS pedidos,
               COUNT(DISTINCT nota_fiscal)              AS notas,
               ROUND(COALESCE(SUM(qtd_pallet), 0) * 1.0
                     / NULLIF(COUNT(DISTINCT {car_exp}), 0), 1) AS media_pallet_carro
        FROM expedicao
        {where}
    """)


# ==========================================================
# LANÇAMENTOS (inserir / listar / apagar / importar / exportar)
# ==========================================================
# Para ajustar os formulários, mexa aqui (campos aceitos) e em
# templates/lancamentos.html (campos mostrados).
# Cada linha lançada guarda quem lançou (lancado_por) e quando (lancado_em).
#
# O campo "recebimento" NÃO é digitado: o sistema gera o número do carro
# automaticamente (veja _numerar_recebimentos).
LANC = {
    "recebimento": {
        "nome": "Recebimento",
        "cabecalho": ["data", "hora", "cliente", "recebimento", "placa", "veiculo"],
        "linha": ["nota_de_transferencia", "nota_de_deposito", "qtd_pallet"],
        "obrigatorios": ["data", "cliente", "nota_de_transferencia"],
    },
    "expedicao": {
        "nome": "Expedição",
        "cabecalho": ["data", "hora", "cliente", "transportadora", "motorista", "romaneio",
                      "placa", "destino", "veiculo"],
        "linha": ["nota_fiscal", "pedido", "qtd_pallet"],
        "obrigatorios": ["data", "cliente", "romaneio", "nota_fiscal"],
    },
}
TIPO_CAMPO = {
    "data": "date", "hora": "hora",
    "qtd_pallet": "int",
    "recebimento": "int", "romaneio": "int",                       # as colunas do banco são INTEGER
    "nota_de_transferencia": "int", "nota_de_deposito": "int", "nota_fiscal": "int",
    "cliente": "upper", "placa": "placa",
}
# sugestões (autocompletar): campo -> [(tabela, coluna), ...]
OPCOES = {
    "cliente": [("recebimento", "cliente"), ("expedicao", "cliente")],
    "transportadora": [("expedicao", "transportadora")],
    "veiculo": [("recebimento", "veiculo"), ("expedicao", "veiculo")],
    "destino": [("expedicao", "destino")],
    "motorista": [("expedicao", "motorista")],
}
EXTRAS = {"lancado_por": "TEXT", "lancado_em": "TEXT"}
PLACA_RE = re.compile(r"^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$")  # ABC1234 (antiga) ou ABC1D23 (Mercosul)
MAX_LINHAS_IMPORTACAO = 5000
MAX_BYTES_IMPORTACAO = 5 * 1024 * 1024


def _col(tab, campo):
    return LANC[tab].get("colunas", {}).get(campo, campo)


def _q(nome):
    return '"' + nome.replace('"', '""') + '"'


def _agora():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _garantir_colunas(conn, tabela):
    # Cria lancado_por / lancado_em nas tabelas que ainda não têm
    existentes = {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}
    for c, t in EXTRAS.items():
        if c not in existentes:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {c} {t}")


def _abrir(tabela):
    conn = sqlite3.connect(CAMINHO_BANCO)
    conn.row_factory = sqlite3.Row
    with conn:
        _garantir_colunas(conn, tabela)
        # placas novas informadas nos lançamentos (para lembrar o veículo nas próximas vezes)
        conn.execute("""CREATE TABLE IF NOT EXISTS veiculos_extra (
                            placa TEXT PRIMARY KEY, veiculo TEXT,
                            atualizado_em TEXT, atualizado_por TEXT)""")
    return conn


def _usuario():
    return session.get("usuario") or ""


def _celula(v):
    # converte o que vem do Excel (datas, horas, números) em texto simples
    import math
    from datetime import date, time
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, time):
        return v.strftime("%H:%M:%S")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v).strip()
    return "" if s.lower() in ("nat", "nan", "none") else s


def _data(v):
    v = v.strip()
    for tam, fmt in ((10, "%Y-%m-%d"), (10, "%d/%m/%Y")):
        try:
            return datetime.strptime(v[:tam], fmt)
        except ValueError:
            pass
    raise ValueError("data inválida")


def remover_acentos(texto):
    # Mesma regra aplicada às planilhas em app.py, para que o que for lançado
    # pelo sistema fique igual ao que veio do Excel (ex.: Armazém -> Armazem).
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


def _valor(campo, bruto):
    v = ("" if bruto is None else str(bruto)).strip()
    tipo = TIPO_CAMPO.get(campo, "text")
    if tipo == "date":
        return _data(v).strftime("%Y-%m-%d 00:00:00") if v else None
    if tipo == "hora":
        if not v:
            return None
        v = v.split(".")[0]
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(v, fmt).strftime("%H:%M:%S")
            except ValueError:
                pass
        raise ValueError("hora inválida")
    if tipo == "int":
        if v == "":
            return None
        try:
            n = int(float(v.replace(",", ".")))
        except ValueError:
            raise ValueError(f"{campo} deve ser um número")
        if n < 0:
            raise ValueError(f"{campo} não pode ser negativo")
        return n
    if tipo == "placa":
        v = re.sub(r"[^A-Za-z0-9]", "", v).upper()
        if not v:
            return None
        if not PLACA_RE.match(v):
            raise ValueError(f"placa inválida ({v})")
        return v
    v = remover_acentos(v)
    if tipo == "upper":
        v = v.upper()
    return v[:120] if v else None


def _registro(tab, bruto):
    # bruto: {campo: texto}. Devolve {coluna_do_banco: valor}, já validado.
    cfg = LANC[tab]
    falta = [c for c in cfg["obrigatorios"] if str(bruto.get(c, "")).strip() == ""]
    if falta:
        raise ValueError("campos obrigatórios: " + ", ".join(falta))
    campos = cfg["cabecalho"] + cfg["linha"]
    rec = {campo: _valor(campo, bruto.get(campo)) for campo in campos}
    return {_col(tab, c): v for c, v in rec.items()}


def _tipo_cadastrado(conn, placa):
    """Procura o veículo de uma placa: 1) placas informadas nos lançamentos (veiculos_extra),
    2) o lançamento mais recente dessa placa em recebimento ou expedição."""
    row = conn.execute("SELECT veiculo FROM veiculos_extra WHERE placa = ?", (placa,)).fetchone()
    if row and row[0]:
        return row[0]
    for tabela in ("recebimento", "expedicao"):
        r = conn.execute(
            f"SELECT veiculo FROM {tabela} WHERE veiculo IS NOT NULL AND TRIM(veiculo) <> '' AND "
            "REPLACE(REPLACE(UPPER(TRIM(placa)), '-', ''), ' ', '') = ? "
            "ORDER BY data DESC LIMIT 1", (placa,)).fetchone()
        if r and str(r[0]).strip():
            return str(r[0]).strip()
    return None


def _resolver_placas(conn, registros, quem, quando):
    """Com placa sem veículo: busca o veículo já conhecido (placa nova -> pede o veículo).
    Com placa e veículo diferentes do conhecido: guarda em veiculos_extra para as próximas vezes."""
    novas = {}
    for rec in registros:
        placa, veiculo = rec.get("placa"), rec.get("veiculo")
        if not placa:
            continue
        if not veiculo:
            veiculo = novas.get(placa) or _tipo_cadastrado(conn, placa)
            if not veiculo:
                raise ValueError(f"placa {placa} ainda não tem veículo cadastrado: informe o veículo")
            rec["veiculo"] = veiculo
        elif novas.get(placa) != veiculo and _tipo_cadastrado(conn, placa) != veiculo:
            novas[placa] = veiculo
    for placa, veiculo in novas.items():
        conn.execute("""INSERT INTO veiculos_extra (placa, veiculo, atualizado_em, atualizado_por)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(placa) DO UPDATE SET veiculo = excluded.veiculo,
                            atualizado_em = excluded.atualizado_em, atualizado_por = excluded.atualizado_por""",
                     (placa, veiculo, quando, quem))


def _numerar_recebimentos(conn, registros, por_grupo):
    """Gera o número do recebimento (= um carro) para os registros que estão sem número.
    - Lançamento manual (por_grupo=False): todas as notas do envio são o mesmo carro.
    - Importação (por_grupo=True): um carro por data + hora + placa + cliente.
    Números que já vieram preenchidos (ex.: da planilha) são mantidos."""
    prox = conn.execute(
        'SELECT COALESCE(MAX(CAST("recebimento" AS INTEGER)), 0) + 1 FROM recebimento'
    ).fetchone()[0]
    grupos = {}
    for rec in registros:
        if rec.get("recebimento") is not None:
            continue
        chave = (rec.get("data"), rec.get("hora"), rec.get("placa"), rec.get("cliente")) if por_grupo else "unico"
        if chave not in grupos:
            grupos[chave] = prox
            prox += 1
        rec["recebimento"] = grupos[chave]


def _inserir(tab, registros, por_grupo=False):
    cols = list(registros[0].keys()) + list(EXTRAS.keys())
    sql = f"INSERT INTO {tab} ({', '.join(_q(c) for c in cols)}) VALUES ({', '.join('?' * len(cols))})"
    quem, quando = _usuario(), _agora()
    conn = _abrir(tab)
    try:
        with conn:
            if tab == "recebimento":
                # trava a escrita para dois usuários não pegarem o mesmo número
                conn.execute("BEGIN IMMEDIATE")
                _numerar_recebimentos(conn, registros, por_grupo)
            _resolver_placas(conn, registros, quem, quando)
            conn.executemany(sql, [tuple(r[c] for c in registros[0]) + (quem, quando) for r in registros])
    finally:
        conn.close()


@sebastian.route("/api/sebastian/lancamentos/opcoes")
def sebastian_lanc_opcoes():
    conn = sqlite3.connect(CAMINHO_BANCO)
    try:
        out = {}
        for campo, fontes in OPCOES.items():
            vals = set()
            for t, col in fontes:
                try:
                    vals.update(r[0] for r in conn.execute(
                        f"SELECT DISTINCT {_q(col)} FROM {t} WHERE {_q(col)} IS NOT NULL AND TRIM({_q(col)}) <> ''"))
                except sqlite3.OperationalError:
                    pass
            out[campo] = sorted(vals, key=lambda x: str(x).lower())
        return jsonify(out)
    finally:
        conn.close()


@sebastian.route("/api/sebastian/lancamentos/<tabela>", methods=["GET"])
def sebastian_lanc_listar(tabela):
    if tabela not in LANC:
        return jsonify(erro="tabela inválida"), 404
    cfg = LANC[tabela]
    campos = list(dict.fromkeys(cfg["cabecalho"] + cfg["linha"]))
    limite = min(max(request.args.get("limite", 40, type=int), 1), 500)
    sel = []
    for c in campos:
        col, tipo = _q(_col(tabela, c)), TIPO_CAMPO.get(c, "text")
        sel.append(f"date({col}) AS {c}" if tipo == "date" else f"{col} AS {c}")
    sql = (f"SELECT rowid AS id, {', '.join(sel)}, lancado_por, lancado_em "
           f"FROM {tabela} ORDER BY rowid DESC LIMIT ?")
    conn = _abrir(tabela)
    try:
        return jsonify([dict(r) for r in conn.execute(sql, [limite]).fetchall()])
    finally:
        conn.close()


@sebastian.route("/api/sebastian/lancamentos/<tabela>", methods=["POST"])
def sebastian_lanc_inserir(tabela):
    if tabela not in LANC:
        return jsonify(erro="tabela inválida"), 404
    cfg = LANC[tabela]
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict):
        return jsonify(erro="envie JSON"), 400
    cab, linhas = corpo.get("cabecalho") or {}, corpo.get("linhas") or []
    if cfg["linha"]:
        if not isinstance(linhas, list) or not linhas or len(linhas) > 200:
            return jsonify(erro="informe de 1 a 200 linhas"), 400
    else:
        linhas = [{}]
    try:
        # "recebimento" vem sempre vazio daqui: o número é gerado em _inserir
        registros = [_registro(tabela, {**cab, **ln, "recebimento": ""}) for ln in linhas]
    except (ValueError, TypeError) as e:
        return jsonify(erro=str(e)), 400
    try:
        _inserir(tabela, registros)
    except ValueError as e:
        return jsonify(erro=str(e)), 400
    return jsonify(ok=True, inseridos=len(registros)), 201


@sebastian.route("/api/sebastian/veiculos/<placa>")
def sebastian_veiculo_por_placa(placa):
    """Veículo já conhecido para a placa (usado pelos formulários de lançamento)."""
    p = re.sub(r"[^A-Za-z0-9]", "", placa).upper()
    if not PLACA_RE.match(p):
        return jsonify(placa=p, valida=False, veiculo=None)
    conn = _abrir("recebimento")
    try:
        veiculo = _tipo_cadastrado(conn, p)
    finally:
        conn.close()
    return jsonify(placa=p, valida=True, veiculo=veiculo)


@sebastian.route("/api/sebastian/lancamentos/<tabela>/<int:rowid>", methods=["PUT"])
def sebastian_lanc_editar(tabela, rowid):
    """Edita uma linha já lançada. Recebe {campo: valor} com os campos da linha
    (cabeçalho + linha). Quem lançou e quando lançou não mudam."""
    if tabela not in LANC:
        return jsonify(erro="tabela inválida"), 404
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict):
        return jsonify(erro="envie JSON"), 400
    try:
        rec = _registro(tabela, corpo)
    except (ValueError, TypeError) as e:
        return jsonify(erro=str(e)), 400
    if tabela == "recebimento" and rec.get("recebimento") is None:
        rec.pop("recebimento", None)  # vazio = mantém o número que já estava
    quem, quando = _usuario(), _agora()
    conn = _abrir(tabela)
    try:
        with conn:
            _resolver_placas(conn, [rec], quem, quando)
            sets = ", ".join(f"{_q(c)} = ?" for c in rec)
            n = conn.execute(f"UPDATE {tabela} SET {sets} WHERE rowid = ?",
                             list(rec.values()) + [rowid]).rowcount
    except ValueError as e:
        return jsonify(erro=str(e)), 400
    finally:
        conn.close()
    return jsonify(ok=bool(n), editados=n), (200 if n else 404)


@sebastian.route("/api/sebastian/lancamentos/<tabela>/<int:rowid>", methods=["DELETE"])
def sebastian_lanc_apagar(tabela, rowid):
    if tabela not in LANC:
        return jsonify(erro="tabela inválida"), 404
    conn = sqlite3.connect(CAMINHO_BANCO)
    try:
        with conn:
            n = conn.execute(f"DELETE FROM {tabela} WHERE rowid = ?", (rowid,)).rowcount
    finally:
        conn.close()
    return jsonify(ok=bool(n), apagados=n), (200 if n else 404)


def _normalizar(nome):
    nome = unicodedata.normalize("NFKD", str(nome)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", nome.lower()).strip("_")


@sebastian.route("/api/sebastian/lancamentos/<tabela>/importar", methods=["POST"])
def sebastian_lanc_importar(tabela):
    """Importa um .xlsx (primeira aba, ou a aba com o nome da tabela). Tudo ou nada:
    se qualquer linha tiver erro, nada é gravado e os erros são listados."""
    if tabela not in LANC:
        return jsonify(erro="tabela inválida"), 404
    arq = request.files.get("arquivo")
    if arq is None or not arq.filename.lower().endswith(".xlsx"):
        return jsonify(erro="envie um arquivo .xlsx"), 400
    dados = arq.read(MAX_BYTES_IMPORTACAO + 1)
    if len(dados) > MAX_BYTES_IMPORTACAO:
        return jsonify(erro="arquivo maior que 5 MB"), 400
    import pandas as pd
    try:
        abas = pd.read_excel(io.BytesIO(dados), sheet_name=None, dtype=object)
    except Exception:
        return jsonify(erro="não foi possível ler o arquivo (é um .xlsx válido?)"), 400
    if not abas:
        return jsonify(erro="arquivo sem abas"), 400
    escolhida = next((n for n in abas if _normalizar(n) == tabela), next(iter(abas)))
    df = abas[escolhida]
    if len(df) > MAX_LINHAS_IMPORTACAO:
        return jsonify(erro=f"máximo de {MAX_LINHAS_IMPORTACAO} linhas por importação"), 400

    cfg = LANC[tabela]
    campos = list(dict.fromkeys(cfg["cabecalho"] + cfg["linha"]))
    alias = {}
    for c in campos:
        alias[_normalizar(c)] = c
        alias[_normalizar(_col(tabela, c))] = c
    # nomes que aparecem na planilha original e viram os campos do banco
    alias.update({"qtd_pallet": "qtd_pallet", "nota_de_transferencia": "nota_de_transferencia",
                  "nota_de_deposito": "nota_de_deposito"})
    alias = {k: v for k, v in alias.items() if v in campos}
    mapa = {col: alias[_normalizar(col)] for col in df.columns if _normalizar(col) in alias}
    faltando = [c for c in cfg["obrigatorios"] if c not in mapa.values()]
    if faltando:
        return jsonify(erro="colunas obrigatórias ausentes na planilha: " + ", ".join(
            _col(tabela, c) for c in faltando)), 400

    registros, erros = [], []
    for i, linha in enumerate(df.to_dict("records"), start=2):  # linha 1 = cabeçalho
        bruto = {mapa[k]: _celula(v) for k, v in linha.items() if k in mapa}
        if not any(bruto.values()):
            continue
        try:
            registros.append(_registro(tabela, bruto))
        except (ValueError, TypeError) as e:
            erros.append(f"linha {i}: {e}")
    if erros:
        return jsonify(erro=f"{len(erros)} linha(s) com problema; nada foi importado",
                       detalhes=erros[:15]), 400
    if not registros:
        return jsonify(erro="nenhuma linha para importar"), 400
    try:
        # por_grupo=True: linhas sem número de recebimento são agrupadas em carros
        _inserir(tabela, registros, por_grupo=True)
    except ValueError as e:
        return jsonify(erro=f"{e}; nada foi importado"), 400
    return jsonify(ok=True, inseridos=len(registros)), 201


# ==========================================================
# EXPORTAR PARA EXCEL (inclui quem lançou cada linha)
# ==========================================================
@sebastian.route("/api/sebastian/exportar")
def sebastian_exportar():
    """?tabelas=recebimento,expedicao (padrão: todas) &ano= &mes="""
    from flask import send_file
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    pedidas = [t for t in (request.args.get("tabelas") or "").split(",") if t in LANC] or list(LANC)
    ano, mes = data()
    wb = Workbook()
    wb.remove(wb.active)
    eh_data = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2}:\d{2})?$")

    def seguro(v):  # evita que texto digitado vire fórmula no Excel
        if not isinstance(v, str) or not v:
            return v
        if v[0] in ("=", "+", "@") or (v[0] == "-" and len(v) > 1 and not v[1:].replace(".", "").isdigit()):
            return "'" + v
        return v

    for tab in pedidas:
        cfg = LANC[tab]
        filtro, params = [], []
        if ano:
            filtro.append("strftime('%Y', data) = ?"); params.append(ano)
        if mes:
            filtro.append("strftime('%m', data) = ?"); params.append(mes)
        where = "WHERE " + " AND ".join(filtro) if filtro else ""
        conn = _abrir(tab)
        try:
            cur = conn.execute(f"SELECT * FROM {tab} {where} ORDER BY rowid", params)
            cabecalho = [d[0] for d in cur.description]
            linhas = cur.fetchall()
        finally:
            conn.close()
        ws = wb.create_sheet(cfg["nome"])
        ws.append(cabecalho)
        for c in ws[1]:
            c.font = Font(bold=True)
        ws.freeze_panes = "A2"
        for r in linhas:
            vals = []
            for nome, v in zip(cabecalho, tuple(r)):
                if isinstance(v, str) and nome.startswith("data") and eh_data.match(v):
                    v = datetime.strptime(v, "%Y-%m-%d %H:%M:%S" if len(v) > 10 else "%Y-%m-%d")
                else:
                    v = seguro(v)
                vals.append(v)
            ws.append(vals)
        for i, nome in enumerate(cabecalho, start=1):
            ws.column_dimensions[get_column_letter(i)].width = min(max(len(nome) + 2, 12), 34)
            if nome.startswith("data"):
                for cel in ws[get_column_letter(i)][1:]:
                    cel.number_format = "DD/MM/YYYY"
            elif nome == "lancado_em":
                for cel in ws[get_column_letter(i)][1:]:
                    cel.number_format = "DD/MM/YYYY HH:MM"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    periodo = f"{ano or 'todos'}" + (f"-{mes}" if mes else "")
    nome = f"sebastian_{pedidas[0] if len(pedidas) == 1 else 'dados'}_{periodo}.xlsx"
    return send_file(buf, as_attachment=True, download_name=nome,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


if __name__ == "__main__":
    app = Flask(__name__)
    app.secret_key = "dev"  # só para testar este arquivo sozinho
    app.register_blueprint(sebastian)
    app.run(debug=True)