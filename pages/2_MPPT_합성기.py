import streamlit as st

def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        st.title("🔒 랩실 전용 페이지")
        pwd = st.text_input("비밀번호를 입력하세요", type="password")

        if pwd:
            if pwd == st.secrets["APP_PASSWORD"]:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
        return False

    return True


if not check_password():
    st.stop()

import io
import hashlib
import importlib.util
import pandas as pd
import streamlit as st
from xml.etree import ElementTree as ET

# =========================
# Page
# =========================
st.title("MPPT / 인버터 / 기상관측 보고서 합성기")
st.caption("여러 파일을 업로드한 뒤 [합성 실행]을 누르면 중복 제거 후 datetime 기준으로 통합본을 생성합니다. (시간 단위 고정)")

# =========================
# Dependency helpers
# =========================
def has_pkg(name: str) -> bool:
    return importlib.util.find_spec(name) is not None

# =========================
# SpreadsheetML(XML) parser (Excel 2003 XML 대응)
# =========================
def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag

def parse_spreadsheetml_xml(xml_bytes: bytes) -> pd.DataFrame:
    root = ET.fromstring(xml_bytes)

    ws = None
    for node in root.iter():
        if _strip_ns(node.tag).lower() == "worksheet":
            ws = node
            break
    if ws is None:
        raise ValueError("XML에서 Worksheet를 찾지 못했습니다.")

    table = None
    for node in ws.iter():
        if _strip_ns(node.tag).lower() == "table":
            table = node
            break
    if table is None:
        raise ValueError("XML에서 Table을 찾지 못했습니다.")

    def get_attr_local(elem, local_name: str):
        for k, v in elem.attrib.items():
            if _strip_ns(k).lower() == local_name.lower():
                return v
        return None

    rows_out = []
    max_cols = 0

    for row in [n for n in table if _strip_ns(n.tag).lower() == "row"]:
        cur = []
        col_pos = 1

        for cell in [n for n in row if _strip_ns(n.tag).lower() == "cell"]:
            idx = get_attr_local(cell, "Index")
            if idx:
                idx = int(idx)
                while col_pos < idx:
                    cur.append(None)
                    col_pos += 1

            data_node = None
            for n in cell:
                if _strip_ns(n.tag).lower() == "data":
                    data_node = n
                    break
            val = data_node.text if data_node is not None else None
            cur.append(val)
            col_pos += 1

            merge_across = get_attr_local(cell, "MergeAcross")
            if merge_across:
                ma = int(merge_across)
                for _ in range(ma):
                    cur.append(None)
                    col_pos += 1

        max_cols = max(max_cols, len(cur))
        rows_out.append(cur)

    for r in rows_out:
        if len(r) < max_cols:
            r.extend([None] * (max_cols - len(r)))

    df = pd.DataFrame(rows_out)
    df.columns = list(range(df.shape[1]))
    return df

# =========================
# Universal reader (내용 기반 자동 판별)
# =========================
def read_report_table(uploaded_file, header=None) -> pd.DataFrame:
    uploaded_file.seek(0)
    head = uploaded_file.read(16)
    uploaded_file.seek(0)

    uploaded_file.seek(0)
    sniff = uploaded_file.read(4096)
    uploaded_file.seek(0)

    sniff_lower = (sniff or b"").lower()
    is_zip = head.startswith(b"PK")
    is_html = (b"<html" in sniff_lower) or (b"<table" in sniff_lower)
    is_xml = (b"<?xml" in sniff_lower[:600]) or (b"<workbook" in sniff_lower[:1200])

    if is_zip:
        if not has_pkg("openpyxl"):
            raise RuntimeError("이 파일은 .xlsx 형식입니다. openpyxl 설치: pip install openpyxl")
        return pd.read_excel(uploaded_file, engine="openpyxl", header=header)

    if is_html:
        uploaded_file.seek(0)
        raw = uploaded_file.read()
        uploaded_file.seek(0)
        text = raw.decode("utf-8", errors="ignore")
        tables = pd.read_html(text)
        if not tables:
            raise ValueError("HTML 테이블을 찾지 못했습니다.")
        df0 = tables[0]
        df0.columns = list(range(df0.shape[1]))
        return df0

    if is_xml:
        uploaded_file.seek(0)
        xml_bytes = uploaded_file.read()
        uploaded_file.seek(0)
        return parse_spreadsheetml_xml(xml_bytes)

    if not has_pkg("xlrd"):
        raise RuntimeError("'.xls' 파일을 읽으려면 xlrd 필요: pip install xlrd==2.0.1")
    return pd.read_excel(uploaded_file, engine="xlrd", header=header)

# =========================
# Utilities
# =========================
def file_key(file_obj) -> str:
    file_obj.seek(0)
    b = file_obj.read()
    file_obj.seek(0)
    h = hashlib.md5(b).hexdigest()
    return f"{file_obj.name}__{len(b)}__{h}"

def extract_datetime_block(df: pd.DataFrame, dt_col: int = 0) -> pd.DataFrame:
    """
    - 0열에서 datetime 변환 가능한 구간만 추출
    - 최대/최소/평균 같은 요약행은 보통 datetime 변환이 안 되므로 자동 제외
    """
    if df is None or df.empty:
        raise ValueError("빈 데이터입니다.")

    dt = pd.to_datetime(df[dt_col], errors="coerce")
    start = dt.first_valid_index()
    end = dt.last_valid_index()

    if start is None or end is None:
        raise ValueError("datetime 컬럼(0열)에서 날짜/시간 데이터 시작행을 찾지 못했습니다.")

    out = df.loc[start:end].copy().reset_index(drop=True)

    out_dt = pd.to_datetime(out[dt_col], errors="coerce")
    out = out[out_dt.notna()].copy().reset_index(drop=True)
    out.insert(0, "_dt_parsed_", pd.to_datetime(out[dt_col], errors="coerce"))
    return out

def detect_datetime_range(df: pd.DataFrame):
    if df is None or df.empty or "datetime" not in df.columns:
        return None, None
    dt = pd.to_datetime(df["datetime"], errors="coerce").dropna()
    if dt.empty:
        return None, None
    return dt.min(), dt.max()

def to_excel_bytes(df: pd.DataFrame, sheet_name="Merged"):
    out = io.BytesIO()
    if not has_pkg("openpyxl"):
        raise RuntimeError("엑셀로 저장하려면 openpyxl이 필요합니다: pip install openpyxl")
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
    out.seek(0)
    return out

def dedup_on_datetime(df: pd.DataFrame) -> pd.DataFrame:
    """datetime 중복 제거: 시간 정렬 후 같은 datetime이면 마지막값 우선"""
    if df is None or df.empty:
        return df
    x = df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
    x = x.dropna(subset=["datetime"])
    x = x.sort_values("datetime")
    x = x.drop_duplicates(subset=["datetime"], keep="last")
    return x.reset_index(drop=True)

def resample_hourly_mean(df: pd.DataFrame) -> pd.DataFrame:
    """시간(H) 단위 고정: 분 단위가 섞여 있으면 시간 평균으로 정규화"""
    if df is None or df.empty:
        return df
    x = df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
    x = x.dropna(subset=["datetime"]).set_index("datetime").sort_index()
    x = x.resample("H").mean(numeric_only=True).reset_index()
    return x

def prefix_df(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    if df is None:
        return None
    cols = []
    for c in df.columns:
        cols.append("datetime" if c == "datetime" else f"{prefix}{c}")
    out = df.copy()
    out.columns = cols
    return out

def fill_missing_hours_and_zero(df: pd.DataFrame) -> pd.DataFrame:
    """
    공백 기간(누락된 시간행)을 생성하고, 수치형 결측(NaN)을 0으로 채웁니다.
    """
    if df is None or df.empty:
        return df

    x = df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
    x = x.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    s = x["datetime"].min().floor("H")
    e = x["datetime"].max().floor("H")
    full = pd.DataFrame({"datetime": pd.date_range(s, e, freq="H")})

    out = full.merge(x, on="datetime", how="left")

    for c in [c for c in out.columns if c != "datetime"]:
        out[c] = pd.to_numeric(out[c], errors="ignore")

    num_cols = out.select_dtypes(include=["number"]).columns.tolist()
    out[num_cols] = out[num_cols].fillna(0)
    return out

# =========================
# Loaders
# =========================
def load_mppt(uploaded_file) -> pd.DataFrame:
    raw = read_report_table(uploaded_file, header=None)
    blk = extract_datetime_block(raw, dt_col=0)

    data = pd.DataFrame()
    data["datetime"] = blk["_dt_parsed_"]

    mapping = [
        ("mppt1_v", 2),
        ("mppt1_a", 3),
        ("mppt2_v", 4),
        ("mppt2_a", 5),
    ]
    for name, idx in mapping:
        if idx < blk.shape[1]:
            data[name] = pd.to_numeric(blk.iloc[:, idx], errors="coerce")

    return data.dropna(subset=["datetime"]).reset_index(drop=True)

def load_inverter(uploaded_file) -> pd.DataFrame:
    raw = read_report_table(uploaded_file, header=None)
    blk = extract_datetime_block(raw, dt_col=0)

    data = pd.DataFrame()
    data["datetime"] = blk["_dt_parsed_"]

    cols_expected = [
        "pv_v", "pv_a", "pv_kw",
        "rs_rn_v", "st_sn_v", "tr_tn_v",
        "freq_hz", "r_a", "s_a", "t_a",
        "inv_kw", "energy_kwh"
    ]
    for i, name in enumerate(cols_expected, start=2):
        if i < blk.shape[1]:
            data[name] = pd.to_numeric(blk.iloc[:, i], errors="coerce")

    return data.dropna(subset=["datetime"]).reset_index(drop=True)

def load_weather(uploaded_file) -> pd.DataFrame:
    raw = read_report_table(uploaded_file, header=None)
    blk = extract_datetime_block(raw, dt_col=0)

    data = pd.DataFrame()
    data["datetime"] = blk["_dt_parsed_"]

    cols_expected = ["inv_kw_weather", "poa_wm2", "ghi_wm2", "module_c", "ambient_c"]
    for i, name in enumerate(cols_expected, start=2):
        if i < blk.shape[1]:
            data[name] = pd.to_numeric(blk.iloc[:, i], errors="coerce")

    return data.dropna(subset=["datetime"]).reset_index(drop=True)

# =========================
# Session storage
# =========================
def init_store():
    if "store" not in st.session_state:
        st.session_state["store"] = {"mppt": {}, "inv": {}, "wea": {}}
    if "merged_df" not in st.session_state:
        st.session_state["merged_df"] = None
    # 업로더 위젯을 강제 초기화하기 위한 리셋 카운터
    if "uploader_reset" not in st.session_state:
        st.session_state["uploader_reset"] = 0

def add_files(kind: str, files, loader):
    if not files:
        return
    for f in files:
        k = file_key(f)
        if k in st.session_state["store"][kind]:
            continue
        try:
            df = loader(f)
            rng = detect_datetime_range(df)
            st.session_state["store"][kind][k] = {"name": f.name, "df": df, "range": rng, "error": None}
        except Exception as e:
            st.session_state["store"][kind][k] = {"name": f.name, "df": None, "range": (None, None), "error": str(e)}

def clear_all():
    # 세션 전체 clear()를 쓰면 업로더가 같은 key로 다시 살아나는 케이스가 있어
    # 필요한 것만 리셋 + 업로더 key를 바꾸는 카운터 증가 방식이 가장 확실합니다.
    st.session_state["store"] = {"mppt": {}, "inv": {}, "wea": {}}
    st.session_state["merged_df"] = None
    st.session_state["uploader_reset"] += 1
    st.rerun()

init_store()

# =========================
# Top controls
# =========================
top_left, top_right = st.columns([2, 1])
with top_left:
    st.info("여러 파일을 누적 업로드할 수 있습니다. (화면 밀림 방지: 상태 목록은 접어서 표시됩니다)")
with top_right:
    if st.button("전체 초기화", type="secondary", use_container_width=True):
        clear_all()

# =========================
# Upload UI (여러 파일)
# =========================
r = st.session_state["uploader_reset"]  # 업로더 강제 초기화용

c1, c2, c3 = st.columns(3)
with c1:
    st.subheader("MPPT (여러 파일)")
    mppt_files = st.file_uploader(
        "MPPT 업로드",
        type=["xls", "xlsx"],
        accept_multiple_files=True,
        key=f"mppt_uploader_{r}",
    )
with c2:
    st.subheader("인버터 (여러 파일)")
    inv_files = st.file_uploader(
        "인버터 업로드",
        type=["xls", "xlsx"],
        accept_multiple_files=True,
        key=f"inv_uploader_{r}",
    )
with c3:
    st.subheader("기상 (여러 파일)")
    wea_files = st.file_uploader(
        "기상 업로드",
        type=["xls", "xlsx"],
        accept_multiple_files=True,
        key=f"wea_uploader_{r}",
    )

add_files("mppt", mppt_files, load_mppt)
add_files("inv", inv_files, load_inverter)
add_files("wea", wea_files, load_weather)

# =========================
# Compact summary (위에서 바로 확인)
# =========================
st.divider()
st.subheader("요약")
mppt_n = len(st.session_state["store"]["mppt"])
inv_n  = len(st.session_state["store"]["inv"])
wea_n  = len(st.session_state["store"]["wea"])
st.write(f"- MPPT 파일: **{mppt_n}개** | 인버터 파일: **{inv_n}개** | 기상 파일: **{wea_n}개**")

# =========================
# Status (접기: 화면 밀림 방지)
# =========================
with st.expander("업로드/인식 상태 보기", expanded=False):
    st.caption("파일이 많아도 화면이 길어지지 않도록 기본은 접혀 있습니다.")

    def render_store(kind: str, label: str):
        items = st.session_state["store"][kind]
        st.markdown(f"### {label} ({len(items)}개)")
        if not items:
            st.info("아직 없음")
            return
        for v in items.values():
            if v["error"]:
                st.error(f"- {v['name']} | 인식 실패: {v['error']}")
            else:
                s, e = v["range"]
                # s/e가 None일 가능성 방어
                if s is None or e is None:
                    st.success(f"- {v['name']} | 기간: (알 수 없음) | rows={len(v['df'])}")
                else:
                    st.success(f"- {v['name']} | 기간: {s:%Y-%m-%d %H:%M} ~ {e:%Y-%m-%d %H:%M} | rows={len(v['df'])}")

    s1, s2, s3 = st.columns(3)
    with s1: render_store("mppt", "MPPT")
    with s2: render_store("inv", "인버터")
    with s3: render_store("wea", "기상")

# =========================
# Merge
# =========================
st.divider()
st.subheader("합성")

def build_merged_hourly():
    def concat_kind(kind: str):
        frames = []
        for v in st.session_state["store"][kind].values():
            if v["df"] is not None and v["error"] is None:
                frames.append(v["df"])
        if not frames:
            return None
        out = pd.concat(frames, ignore_index=True)
        out = dedup_on_datetime(out)
        out = resample_hourly_mean(out)  # 시간 단위 고정
        out = dedup_on_datetime(out)     # 안전
        return out

    mppt = concat_kind("mppt")
    inv  = concat_kind("inv")
    wea  = concat_kind("wea")

    mppt = prefix_df(mppt, "mppt_")
    inv  = prefix_df(inv,  "inv_")
    wea  = prefix_df(wea,  "wea_")

    dfs = [d for d in [mppt, inv, wea] if d is not None]
    if not dfs:
        return None

    out = dfs[0]
    for d in dfs[1:]:
        out = out.merge(d, on="datetime", how="outer")

    out = out.sort_values("datetime").reset_index(drop=True)
    return out

b1, b2 = st.columns([1, 1])
with b1:
    if st.button("합성 실행", type="primary", use_container_width=True):
        merged = build_merged_hourly()
        st.session_state["merged_df"] = merged
        if merged is None or merged.empty:
            st.error("합성할 데이터가 없습니다.")
        else:
            s, e = detect_datetime_range(merged)
            if s is None or e is None:
                st.success(f"합성 완료 | rows={len(merged)}")
            else:
                st.success(f"합성 완료 | 기간: {s:%Y-%m-%d %H:%M} ~ {e:%Y-%m-%d %H:%M} | rows={len(merged)}")

with b2:
    if st.button("공백 기간 0 추가", use_container_width=True):
        merged = st.session_state.get("merged_df")
        if merged is None or merged.empty:
            st.warning("먼저 [합성 실행]으로 통합본을 만든 뒤 눌러주세요.")
        else:
            filled = fill_missing_hours_and_zero(merged)
            st.session_state["merged_df"] = filled
            s, e = detect_datetime_range(filled)
            if s is None or e is None:
                st.success(f"공백 시간행 생성 + 결측 0 채움 완료 | rows={len(filled)}")
            else:
                st.success(f"공백 시간행 생성 + 결측 0 채움 완료 | 기간: {s:%Y-%m-%d %H:%M} ~ {e:%Y-%m-%d %H:%M} | rows={len(filled)}")

# =========================
# Download merged
# =========================
st.divider()
st.subheader("통합본 다운로드")

merged_df = st.session_state.get("merged_df")
if merged_df is None or merged_df.empty:
    st.info("아직 합성된 통합본이 없습니다. [합성 실행]을 눌러주세요.")
else:
    s, e = detect_datetime_range(merged_df)
    if s is not None and e is not None:
        st.write(f"- 통합 기간: {s:%Y-%m-%d %H:%M} ~ {e:%Y-%m-%d %H:%M}")
    st.write(f"- 행 수: {len(merged_df)} / 컬럼 수: {merged_df.shape[1]}")

    st.download_button(
        "통합본 다운로드(.xlsx)",
        data=to_excel_bytes(merged_df, "Merged"),
        file_name="통합_정리본.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )

