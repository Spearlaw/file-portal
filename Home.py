import streamlit as st

# =========================
# Password check
# =========================
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if not st.session_state.authenticated:
        st.title("🔒 랩실 전용 페이지")
        st.write("랩실 구성원만 접근 가능합니다.")
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

# =========================
# Home page
# =========================
st.title("📂 파일 합성기 포털")
st.caption("왼쪽 메뉴에서 사용할 기능을 선택하세요.")

st.markdown("""
### 사용 가능한 기능
- **Smart File Unifier**  
  여러 파일을 시간 기준으로 정리·통합합니다.

- **MPPT 합성기**  
  MPPT 및 실험 데이터를 통합 처리합니다.
""")

st.info("문의 사항은 랩실 관리자에게 연락하세요.")
