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

import streamlit as st
import pandas as pd
import io
from datetime import datetime, time
import re
import csv
from typing import Optional, Tuple, List, Any, Dict

st.title("🚀 Smart File Unifier")

# =============================
# (추가) 1분 대용량 보호 설정
# =============================
# - 1분 + 공백채우기(reindex) 시 행 수가 폭증하면 공유서버에서 튕길 확률이 큽니다.
# - 아래 임계값은 보수적으로 잡았습니다. 필요하면 조절하세요.
FILL_ROW_LIMIT_1MIN = 200_000          # 1분 공백채우기 허용 최대 행수 (약 139일 분량)
XLSX_ROW_LIMIT_WARN = 150_000          # 엑셀 저장이 위험해지기 시작하는 행수(경고/CSV 권장)

# -----------------------------
# uploader reset key / confirm
# -----------------------------
if "uploader_key" not in st.session_state:
    st.session_state["uploader_key"] = 0
if "confirm_reset" not in st.session_state:
    st.session_state["confirm_reset"] = False

def safe_rerun():
    if hasattr(st, "rerun"):
        st.rerun()
    elif hasattr(st, "experimental_rerun"):
        st.experimental_rerun()
    return

# -----------------------------
# 전체 제거 버튼(확인 포함)
# -----------------------------
cbtn, _ = st.columns([1, 6])
with cbtn:
    if st.button("🗑 전체 제거"):
        st.session_state["confirm_reset"] = True

if st.session_state["confirm_reset"]:
    st.warning("업로드된 파일과 현재 분석 결과가 모두 제거됩니다. 계속하시겠습니까?")
    y, n = st.columns([1, 1])
    with y:
        if st.button("✅ 예, 모두 제거"):
            st.session_state["uploader_key"] += 1
            st.session_state.pop("combined_df", None)
            st.session_state.pop("filtered_df", None)
            st.session_state.pop("upload_signature", None)
            st.session_state["confirm_reset"] = False
            safe_rerun()
    with n:
        if st.button("❌ 취소"):
            st.session_state["confirm_reset"] = False

uploaded_files = st.file_uploader(
    "파일을 한꺼번에 업로드하세요",
    accept_multiple_files=True,
    key=f"uploader_{st.session_state['uploader_key']}"
)

# -----------------------------
# time parse
# -----------------------------
def parse_hhmm(s: str, *, allow_2400: bool = False):
    if s is None:
        return None, "시간 입력이 비어 있습니다."
    s = s.strip()
    if not re.fullmatch(r"\d{1,2}:\d{2}", s):
        return None, "형식 오류: HH:MM 형태로 입력해 주세요. 예) 09:30, 0:05"

    hh, mm = s.split(":")
    hh = int(hh); mm = int(mm)

    if mm < 0 or mm > 59:
        return None, "분(mm)은 00~59 범위여야 합니다."

    if hh == 24 and mm == 0 and allow_2400:
        return time(23, 59, 59), None

    if hh < 0 or hh > 23:
        return None, "시(HH)는 00~23 범위여야 합니다. (종료 시간만 24:00 허용)"

    return time(hh, mm, 0), None


# -----------------------------
# robust loader (format-agnostic)
# -----------------------------
TS_CANDIDATES = [
    "TIMESTAMP", "Timestamp", "timestamp",
    "DateTime", "DATETIME", "DATE_TIME", "DATE TIME",
    "Time", "TIME", "Date", "DATE"
]

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df.columns = [str(c).strip() for c in df.columns]
    if "TIMESTAMP" not in df.columns:
        upper_map = {str(c).strip().upper(): c for c in df.columns}
        for cand in TS_CANDIDATES:
            key = cand.upper()
            if key in upper_map:
                df = df.rename(columns={upper_map[key]: "TIMESTAMP"})
                break
    return df

def decode_text_best_effort(file_bytes: bytes) -> Tuple[str, str]:
    for enc in ["utf-8-sig", "utf-8", "cp949"]:
        try:
            return file_bytes.decode(enc, errors="strict"), enc
        except Exception:
            pass
    return file_bytes.decode("utf-8", errors="replace"), "utf-8(replace)"

def sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:80])
    try:
        d = csv.Sniffer().sniff(sample, delimiters=[",", "\t", ";", "|"])
        return d.delimiter
    except Exception:
        if "\t" in sample: return "\t"
        if ";" in sample: return ";"
        if "|" in sample: return "|"
        return ","

def find_header_line_index(text: str, max_lines: int = 300) -> Optional[int]:
    lines = text.splitlines()
    upper_candidates = [c.upper() for c in TS_CANDIDATES]
    for i, line in enumerate(lines[:max_lines]):
        u = line.upper()
        if any(c in u for c in upper_candidates):
            return i
    return None

def postprocess_df(df: pd.DataFrame) -> pd.DataFrame:
    df = normalize_columns(df)
    if "TIMESTAMP" in df.columns:
        df["TIMESTAMP"] = pd.to_datetime(df["TIMESTAMP"], errors="coerce")
        df = df.dropna(subset=["TIMESTAMP"])
    return df

def score_timestamp_quality(df: Optional[pd.DataFrame]) -> int:
    if df is None or df.empty: return -10**9
    if "TIMESTAMP" not in df.columns: return -10**9
    ts = pd.to_datetime(df["TIMESTAMP"], errors="coerce")
    good = int(ts.notna().sum())
    total = int(len(ts))
    if total == 0 or good == 0: return -10**9
    ratio = good / total
    uniq = int(ts.dropna().nunique())
    mono = int(ts.dropna().is_monotonic_increasing)
    return int(ratio * 1_000_000) + good * 10 + uniq + mono * 1000

def try_read_csv_variant(file_bytes: bytes, encoding: str, delimiter: str,
                         header: Any, skiprows: Optional[List[int]] = None) -> Optional[pd.DataFrame]:
    try:
        return pd.read_csv(
            io.BytesIO(file_bytes),
            encoding=encoding,
            delimiter=delimiter,
            header=header,
            skiprows=skiprows,
            engine="python",
        )
    except Exception:
        return None

def try_read_excel_variant(file_bytes: bytes, skiprows: Optional[List[int]] = None,
                           header: Any = 0) -> Optional[pd.DataFrame]:
    try:
        return pd.read_excel(io.BytesIO(file_bytes), skiprows=skiprows, header=header)
    except Exception:
        return None

def pick_best_dataframe(candidates: List[Tuple[str, Optional[pd.DataFrame]]]) -> Optional[pd.DataFrame]:
    best_df = None
    best_score = -10**18
    for _, raw in candidates:
        if raw is None:
            continue
        df = postprocess_df(raw)
        sc = score_timestamp_quality(df)
        if sc > best_score:
            best_score = sc
            best_df = df
    return best_df

def read_any_file_from_bytes(file_bytes: bytes, filename: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
    ext = filename.lower().split(".")[-1] if "." in filename else ""

    if ext == "xls":
        return None, "현재 버전에서는 보고서형 .xls 취합을 제외했습니다."

    if ext in ["xlsx", "xlsm", "xltx", "xltm"]:
        candidates: List[Tuple[str, Optional[pd.DataFrame]]] = []
        candidates.append(("xlsx_header0", try_read_excel_variant(file_bytes, skiprows=None, header=0)))
        candidates.append(("xlsx_skip_0_2_3", try_read_excel_variant(file_bytes, skiprows=[0, 2, 3], header=0)))

        head = try_read_excel_variant(file_bytes, skiprows=None, header=None)
        if head is not None and not head.empty:
            max_rows = min(300, len(head))
            header_idx = None
            for i in range(max_rows):
                row = head.iloc[i].astype(str).str.upper().tolist()
                if any(c.upper() in " ".join(row) for c in TS_CANDIDATES):
                    header_idx = i
                    break
            if header_idx is not None and header_idx > 0:
                candidates.append((f"xlsx_header_at_{header_idx}",
                                   try_read_excel_variant(file_bytes, skiprows=list(range(header_idx)), header=0)))

        best = pick_best_dataframe(candidates)
        if best is None:
            return None, "엑셀 파싱 실패: TIMESTAMP를 찾지 못했습니다."
        return best, None

    text, enc = decode_text_best_effort(file_bytes)
    delim = sniff_delimiter(text)
    header_idx = find_header_line_index(text)

    candidates_t: List[Tuple[str, Optional[pd.DataFrame]]] = []
    candidates_t.append(("csv_header0", try_read_csv_variant(file_bytes, enc, delim, header=0, skiprows=None)))
    if header_idx is not None and header_idx > 0:
        candidates_t.append((f"csv_header_at_{header_idx}",
                             try_read_csv_variant(file_bytes, enc, delim, header=0, skiprows=list(range(header_idx)))))
    candidates_t.append(("csv_skip_0_2_3", try_read_csv_variant(file_bytes, enc, delim, header=0, skiprows=[0, 2, 3])))

    for alt in [",", "\t", ";", "|"]:
        if alt == delim:
            continue
        candidates_t.append((f"csv_header0_delim_{repr(alt)}",
                             try_read_csv_variant(file_bytes, enc, alt, header=0, skiprows=None)))
        candidates_t.append((f"csv_skip_0_2_3_delim_{repr(alt)}",
                             try_read_csv_variant(file_bytes, enc, alt, header=0, skiprows=[0, 2, 3])))
        if header_idx is not None and header_idx > 0:
            candidates_t.append((f"csv_header_at_{header_idx}_delim_{repr(alt)}",
                                 try_read_csv_variant(file_bytes, enc, alt, header=0, skiprows=list(range(header_idx)))))

    best = pick_best_dataframe(candidates_t)
    if best is None:
        return None, f"CSV/DAT 파싱 실패: TIMESTAMP를 찾지 못했습니다. (encoding={enc})"
    return best, None


# -----------------------------
# dedup / conflicts / fill
# -----------------------------
def get_value_cols(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if c not in ("TIMESTAMP", "RECORD")]

def drop_exact_duplicates_excluding_record(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "TIMESTAMP" not in df.columns:
        return df
    value_cols = [c for c in get_value_cols(df) if c in df.columns]
    subset = ["TIMESTAMP"] + value_cols
    return df.drop_duplicates(subset=subset, keep="last").copy()

def resolve_timestamp_conflicts_most_non_null(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "TIMESTAMP" not in df.columns:
        return df
    value_cols = [c for c in get_value_cols(df) if c in df.columns]
    d = df.copy()
    d["_nn"] = d[value_cols].notna().sum(axis=1) if value_cols else 0
    d["_rowid"] = range(len(d))
    d = d.sort_values(["TIMESTAMP", "_nn", "_rowid"])
    picked = d.groupby("TIMESTAMP", as_index=False).tail(1).drop(columns=["_nn", "_rowid"])
    return picked.sort_values("TIMESTAMP").copy()

def fill_missing_by_reindex(df: pd.DataFrame, start_dt: datetime, end_dt: datetime, freq: str) -> pd.DataFrame:
    full_range = pd.date_range(start=pd.Timestamp(start_dt), end=pd.Timestamp(end_dt), freq=freq)
    if df.empty:
        return pd.DataFrame({"TIMESTAMP": full_range})
    d = df.copy().sort_values("TIMESTAMP").set_index("TIMESTAMP")
    d = d.reindex(full_range)
    d.index.name = "TIMESTAMP"
    return d.reset_index()

def looks_numeric_series(s: pd.Series, sample_n: int = 200, threshold: float = 0.85) -> bool:
    x = s.dropna()
    if x.empty:
        return False
    if len(x) > sample_n:
        x = x.sample(sample_n, random_state=1)
    if pd.api.types.is_numeric_dtype(x):
        return True
    xs = x.astype(str).str.strip()
    xs = xs[xs != ""]
    if xs.empty:
        return False
    conv = pd.to_numeric(xs, errors="coerce")
    success = conv.notna().mean()
    return (success >= threshold) and (conv.notna().sum() > 0)

def fill_zeros_for_numeric_like_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    value_cols = [c for c in df.columns if c not in ("TIMESTAMP", "RECORD")]
    numeric_like = []
    for c in value_cols:
        try:
            if looks_numeric_series(df[c]):
                numeric_like.append(c)
        except Exception:
            continue

    out = df.copy()
    for c in numeric_like:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)

    return out

# ✅ (추가) 1일 간격 선택 시 날짜 경계로 정규화
def normalize_to_day_bounds(start_dt: datetime, end_dt: datetime) -> Tuple[datetime, datetime]:
    s = datetime.combine(start_dt.date(), time(0, 0, 0))
    e = datetime.combine(end_dt.date(), time(23, 59, 59))
    return s, e

# ✅ (추가) 선택 간격 기준 예상 행 수 계산(공백 채우기 안전장치용)
def estimate_rows(start_dt: datetime, end_dt: datetime, freq_label: str) -> int:
    seconds = (end_dt - start_dt).total_seconds()
    if seconds < 0:
        return 0
    if freq_label == "1분":
        step = 60
    elif freq_label == "10분":
        step = 600
    elif freq_label == "1시간":
        step = 3600
    elif freq_label == "1일":
        step = 86400
    else:
        step = 60
    return int(seconds // step) + 1


# -----------------------------
# main
# -----------------------------
if uploaded_files:
    current_signature = tuple((f.name, getattr(f, "size", None)) for f in uploaded_files)

    if "combined_df" not in st.session_state or st.session_state.get("upload_signature") != current_signature:
        all_dfs: List[pd.DataFrame] = []
        failed: List[Tuple[str, str]] = []
        success: List[str] = []
        file_schema: Dict[str, List[str]] = {}

        st.write("### ⏳ 파일 로딩 진행")

        PARSE_WEIGHT = 0.35
        sizes = [(f.name, int((getattr(f, "size", 0) or 0))) for f in uploaded_files]
        total_units = sum(int(sz * (1.0 + PARSE_WEIGHT)) for _, sz in sizes) or 1
        done_units = 0

        progress_bar = st.progress(0)
        progress_text = st.empty()

        def set_progress(note: str = ""):
            pct = int(done_units / total_units * 100)
            pct = max(0, min(100, pct))
            progress_bar.progress(pct)
            if note:
                progress_text.write(f"{pct}% - {note}")

        total = len(uploaded_files)

        for idx, f in enumerate(uploaded_files, start=1):
            sz = int((getattr(f, "size", 0) or 0))
            set_progress(f"처리 중: {f.name} ({idx}/{total})")

            file_bytes = f.getvalue()
            done_units += int(len(file_bytes) * 1.0)
            set_progress(f"{f.name} 읽기 완료")

            df, err = read_any_file_from_bytes(file_bytes, f.name)
            done_units += int(sz * PARSE_WEIGHT)

            if err or df is None:
                failed.append((f.name, err or "알 수 없는 오류"))
            else:
                # =============================
                # (추가) 파일 단위 선정리(기능 유지 + 대용량 안정성↑)
                # - 기존에 최종 단계에서 하던 정리와 동일한 로직을 "파일별로도" 한 번 수행
                # - 최종 concat 후에도 기존대로 한 번 더 수행하므로 안전망 유지
                # =============================
                if "TIMESTAMP" in df.columns and not df.empty:
                    df = df.sort_values("TIMESTAMP").reset_index(drop=True)
                    df = drop_exact_duplicates_excluding_record(df)
                    df = resolve_timestamp_conflicts_most_non_null(df)
                    df = df.sort_values("TIMESTAMP").reset_index(drop=True)

                all_dfs.append(df)
                success.append(f.name)
                file_schema[f.name] = list(df.columns)

        done_units = total_units
        set_progress("완료")

        st.write("### ✅ 로드 결과 요약")
        st.write(f"- 전체: **{total}개** | 성공: **{len(success)}개** | 실패: **{len(failed)}개**")

        if failed:
            with st.expander("실패한 파일 보기(원인 포함)"):
                for n, e in failed:
                    st.write(f"- ❌ {n}: {e}")

        with st.expander("파일별 컬럼(스키마) 확인"):
            for n, cols in file_schema.items():
                st.write(f"- **{n}**: {cols}")

        if all_dfs:
            combined_df = pd.concat(all_dfs, axis=0, ignore_index=True, sort=False)

            # (기존 기능 유지) 최종 통합본에서도 동일 정리 1회 수행
            if "TIMESTAMP" in combined_df.columns:
                combined_df = combined_df.sort_values("TIMESTAMP").reset_index(drop=True)
                combined_df = drop_exact_duplicates_excluding_record(combined_df)
                combined_df = resolve_timestamp_conflicts_most_non_null(combined_df)
                combined_df = combined_df.sort_values("TIMESTAMP").reset_index(drop=True)

            st.session_state["combined_df"] = combined_df
            st.session_state["upload_signature"] = current_signature
            st.session_state.pop("filtered_df", None)
        else:
            st.session_state["combined_df"] = pd.DataFrame()
            st.session_state["upload_signature"] = current_signature
            st.session_state.pop("filtered_df", None)

    combined_df = st.session_state["combined_df"]

    if combined_df is None or len(combined_df) == 0:
        st.warning("유효하게 로드된 데이터가 없습니다. (헤더/구분자/인코딩/TIMESTAMP 등을 확인해 주세요.)")
    else:
        recognized_min = recognized_max = None
        if "TIMESTAMP" in combined_df.columns and len(combined_df) > 0:
            recognized_min = combined_df["TIMESTAMP"].min()
            recognized_max = combined_df["TIMESTAMP"].max()

        st.write("## 📌 통합 결과 요약")
        st.write(f"- 입력 파일 개수: **{len(uploaded_files)}개**")
        st.write(f"- 통합 행 수(정리 후): **{len(combined_df)}행**")
        if recognized_min is not None and recognized_max is not None:
            st.write(f"- 인식 기간: **{recognized_min:%Y-%m-%d %H:%M:%S} ~ {recognized_max:%Y-%m-%d %H:%M:%S}**")
        else:
            st.warning("TIMESTAMP가 없어 인식 기간/기간 필터/공백 채우기 기능이 제한됩니다.")

        if recognized_min is not None and recognized_max is not None:
            st.write("### 🧭 데이터 설정 (적용 버튼을 눌러야 반영됩니다)")
            with st.form("settings_form", clear_on_submit=False):
                c1, c2, c3, c4 = st.columns([1, 1, 1, 1])
                with c1:
                    start_date = st.date_input("시작 날짜", value=recognized_min.date())
                with c2:
                    start_time_str = st.text_input("시작 시간(HH:MM)", value=recognized_min.strftime("%H:%M"))
                with c3:
                    end_date = st.date_input("종료 날짜", value=recognized_max.date())
                with c4:
                    end_time_str = st.text_input("종료 시간(HH:MM)", value=recognized_max.strftime("%H:%M"))

                st.write("#### 🧩 시계열 공백 0 채우기")
                freq_map = {"1분": "1T", "10분": "10T", "1시간": "1H", "1일": "1D"}
                freq_label = st.selectbox("데이터 간격(공백 채우기 기준)", ["1분", "10분", "1시간", "1일"], index=2)

                # =============================
                # (추가) 1분 모드 안전장치 안내
                # =============================
                if freq_label == "1분":
                    st.info(
                        f"⚠️ 1분 단위는 데이터가 매우 커질 수 있어 공유 서버에서 튕길 수 있습니다.\n"
                        f"- 공백 채우기(리인덱스)는 선택 기간이 커지면 제한될 수 있습니다.\n"
                        f"- 임계값: 약 {FILL_ROW_LIMIT_1MIN:,}행(1분 기준) 초과 시 차단"
                    )
                fill_missing = st.checkbox("선택 기간 내 누락된 시간을 0으로 채우기", value=True)

                apply_btn = st.form_submit_button("✅ 적용")

            if apply_btn:
                start_time, err1 = parse_hhmm(start_time_str, allow_2400=False)
                end_time, err2 = parse_hhmm(end_time_str, allow_2400=True)
                if err1: st.error(f"시작 시간 오류: {err1}")
                if err2: st.error(f"종료 시간 오류: {err2}")

                if (not err1) and (not err2):
                    start_dt = datetime.combine(start_date, start_time)
                    end_dt = datetime.combine(end_date, end_time)

                    if freq_label == "1일":
                        start_dt, end_dt = normalize_to_day_bounds(start_dt, end_dt)

                    if start_dt > end_dt:
                        st.error("기간 선택 오류: 시작이 종료보다 늦습니다.")
                    else:
                        filtered_df = combined_df[
                            (combined_df["TIMESTAMP"] >= pd.Timestamp(start_dt)) &
                            (combined_df["TIMESTAMP"] <= pd.Timestamp(end_dt))
                        ].copy()

                        # (기존 기능 유지) 기간 필터 후 정리
                        filtered_df = drop_exact_duplicates_excluding_record(filtered_df)
                        filtered_df = resolve_timestamp_conflicts_most_non_null(filtered_df)

                        st.write(f"- 선택 기간: **{start_dt:%Y-%m-%d %H:%M:%S} ~ {end_dt:%Y-%m-%d %H:%M:%S}**")
                        st.write(f"- 선택 기간 내 실제 데이터 행 수(정리 후): **{len(filtered_df)}행**")

                        if fill_missing:
                            # =============================
                            # (추가) 1분 공백채우기 보호장치
                            # =============================
                            est = estimate_rows(start_dt, end_dt, freq_label)
                            if freq_label == "1분" and est > FILL_ROW_LIMIT_1MIN:
                                st.error(
                                    f"1분 단위 공백 채우기는 선택 기간이 너무 깁니다.\n"
                                    f"- 예상 행 수: {est:,}행\n"
                                    f"- 허용 한도: {FILL_ROW_LIMIT_1MIN:,}행\n"
                                    f"기간을 줄이거나, 공백 채우기를 끄고 진행해 주세요."
                                )
                                # 공백 채우기만 스킵하고 결과 저장은 진행
                                st.session_state["filtered_df"] = filtered_df
                            else:
                                freq = freq_map[freq_label]
                                filled_df = fill_missing_by_reindex(filtered_df, start_dt, end_dt, freq)
                                filled_df = fill_zeros_for_numeric_like_columns(filled_df)
                                filtered_df = filled_df
                                st.success(f"공백을 0으로 채웠습니다. (간격: {freq_label})")
                                st.session_state["filtered_df"] = filtered_df
                        else:
                            st.session_state["filtered_df"] = filtered_df

        display_df = st.session_state.get("filtered_df", combined_df)

        st.write("### 📊 통합 데이터 미리보기")
        st.dataframe(display_df.head(10), use_container_width=True)

        st.write("### 📥 다운로드")

        # =============================
        # (추가) 대용량 다운로드 안전장치(기존 Excel 유지 + CSV 옵션 추가)
        # =============================
        row_cnt = int(len(display_df))
        if row_cnt >= XLSX_ROW_LIMIT_WARN:
            st.warning(
                f"현재 데이터가 {row_cnt:,}행입니다. 공유 서버에서 Excel(.xlsx) 생성 중 튕길 수 있어 CSV 다운로드를 권장합니다."
            )

        download_fmt = st.radio(
            "다운로드 형식",
            options=["Excel(.xlsx)", "CSV(.csv)"],
            index=0 if row_cnt < XLSX_ROW_LIMIT_WARN else 1,
            horizontal=True
        )

        default_base = "Merged_Data_Output"
        file_name_input = st.text_input("저장 파일명(확장자 제외)", value=default_base).strip()
        if not file_name_input:
            file_name_input = default_base

        if download_fmt == "CSV(.csv)":
            csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label="📥 통합 데이터 다운로드 (CSV)",
                data=csv_bytes,
                file_name=f"{file_name_input}.csv",
                mime="text/csv"
            )
        else:
            # Excel 다운로드(기존 기능 유지)
            output = io.BytesIO()
            try:
                with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                    display_df.to_excel(writer, index=False)
                st.download_button(
                    label="📥 통합 데이터 다운로드 (Excel)",
                    data=output.getvalue(),
                    file_name=f"{file_name_input}.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )
            except Exception as e:
                st.error("Excel 파일 생성 중 오류가 발생했습니다. CSV로 다운로드를 권장합니다.")
                st.exception(e)
                csv_bytes = display_df.to_csv(index=False).encode("utf-8-sig")
                st.download_button(
                    label="📥 통합 데이터 다운로드 (CSV로 대체)",
                    data=csv_bytes,
                    file_name=f"{file_name_input}.csv",
                    mime="text/csv"
                )
