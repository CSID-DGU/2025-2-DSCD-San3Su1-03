import os
import time
import bcrypt
from datetime import datetime
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from dotenv import load_dotenv
import streamlit as st
from pillow_heif import register_heif_opener
register_heif_opener()



# ---------------------------------------------------------
# 0) DB 접속 정보 불러오기 (secrets 우선, 없으면 환경변수 사용)
# ---------------------------------------------------------
def get_db_url():
    if "postgres" in st.secrets:
        cfg = st.secrets["postgres"]
        host = cfg.get("host")
        port = cfg.get("port", 5432)
        user = cfg.get("user")
        password = cfg.get("password")
        database = cfg.get("database")
    else:
        load_dotenv()
        host = os.getenv("PGHOST")
        port = os.getenv("PGPORT", "5432")
        user = os.getenv("PGUSER")
        password = os.getenv("PGPASSWORD")
        database = os.getenv("PGDATABASE")

    assert all([host, port, user, password, database]), "DB 접속정보가 없습니다."
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"

@st.cache_resource
def get_engine():
    engine = create_engine(get_db_url(), pool_pre_ping=True)
    # 연결 확인
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    return engine


# ---------------------------------------------------------
# 2) 회원가입/로그인 비즈니스 로직
# ---------------------------------------------------------
def hash_password(plain: str) -> str:
    # bcrypt는 내부적으로 솔트를 포함 → 같은 비번도 매번 다른 해시
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False

def create_user(engine, email: str, password: str) -> bool:
    if not email or not password:
        raise ValueError("이메일/비밀번호는 비어 있을 수 없습니다.")
    pwd_hash = hash_password(password)
    sql = "INSERT INTO users (email, password_hash) VALUES (:email, :pwd)"
    try:
        with engine.begin() as conn:
            conn.execute(text(sql), {"email": email.lower().strip(), "pwd": pwd_hash})
        return True
    except IntegrityError:
        # UNIQUE(email) 위반
        return False

def get_user_by_email(engine, email: str):
    sql = "SELECT user_id, email, password_hash, created_at FROM users WHERE email = :email"
    with engine.begin() as conn:
        row = conn.execute(text(sql), {"email": email.lower().strip()}).fetchone()
        return dict(row._mapping) if row else None

# ---------------------------------------------------------
# 3) 간단한 세션 관리
# ---------------------------------------------------------
def login_user(user_dict: dict):
    st.session_state["auth"] = {
        "user_id": user_dict["user_id"],
        "email": user_dict["email"],
        "login_at": datetime.utcnow().isoformat()
    }

def logout_user():
    st.session_state.pop("auth", None)

def is_logged_in():
    return "auth" in st.session_state

# ---------------------------------------------------------
# 3-1) 커스텀 사이드바 렌더 함수
# ---------------------------------------------------------
def render_sidebar():
    with st.sidebar:
        if not is_logged_in():
            # 로그인 전: 사이드바 비움
            st.empty()
            return

        st.markdown("### 메뉴")
        # 파일명은 너 프로젝트의 실제 파일명/경로에 맞춰 수정!
        # st.page_link가 있으면 그걸 추천, 없으면 st.button + st.switch_page 사용
        try:
            st.page_link("pages/01_MyPage.py", label="My page", icon="👤")
            st.page_link("pages/02_Route.py", label="Route visualization", icon="🗺️")
            st.page_link("pages/03_Summary.py", label="AI summary", icon="📝")
            st.page_link("pages/04_NextRec.py", label="Next recommendation", icon="✨")
        except Exception:
            # 구버전 Streamlit이면 버튼 + switch_page로 대체
            if st.button("👤 My Page"): st.switch_page("pages/01_MyPage.py")
            if st.button("🗺️ Route Visualization"): st.switch_page("pages/02_Route.py")
            if st.button("📝 AI Summary"): st.switch_page("pages/03_Summary.py")
            if st.button("✨ Next Recommendation"): st.switch_page("pages/04_NextRec.py")

        st.divider()
        if st.button("로그아웃"):
            st.success("로그아웃 되었습니다.")
            time.sleep(2)
            logout_user()
            st.rerun()


def main():
    st.set_page_config(page_title="Life-Recorder Demo", page_icon="📍", layout="centered")
    st.markdown("""
        <style>
        [data-testid="stSidebarNav"] { display: none; }
        </style>
    """, unsafe_allow_html=True)

    # 0) 이미 로그인 → 업로드로
    if is_logged_in():
        st.switch_page("pages/00_Upload.py")
        st.stop()

    # 1) 직전 사이클에서 리다이렉트 예정이면 바로 이동
    if st.session_state.pop("login_redirect", False):
        st.switch_page("pages/00_Upload.py")
        st.stop()

    # 2) '로그인 시도'가 올라왔으면, 탭 만들기 전에 인증 처리
    pending = st.session_state.pop("_pending_login", None)
    if pending:
        eng_auth = get_engine()  # ← 이름 다르게
        user = get_user_by_email(eng_auth, pending["email"])
        if (user is not None) and verify_password(pending["pw"], user["password_hash"]):
            login_user(user)
            st.session_state["login_redirect"] = True
            st.rerun()
        else:
            st.session_state["_login_error"] = "이메일 또는 비밀번호가 올바르지 않습니다."

    st.title("📍Life Recorder (Streamlit + PostgreSQL)📍")

    # 3) 미로그인일 때만 탭 렌더
    if not is_logged_in():
        if st.session_state.pop("_login_error", None):
            st.error("이메일 또는 비밀번호가 올바르지 않습니다.")

        tab_login, tab_signup = st.tabs(["로그인", "회원가입"])

        # ---------------- 회원가입 ----------------
        with tab_signup:
            st.subheader("회원가입")
            with st.form("signup_form", clear_on_submit=False):
                new_email = st.text_input("이메일", placeholder="you@example.com")
                new_pw = st.text_input("비밀번호", type="password")
                new_pw2 = st.text_input("비밀번호 확인", type="password")
                submitted_signup = st.form_submit_button("회원가입")
            if submitted_signup:
                if new_pw != new_pw2:
                    st.error("비밀번호 확인이 일치하지 않습니다.")
                elif len(new_pw) < 8:
                    st.error("비밀번호는 8자 이상을 권장합니다.")
                else:
                    eng_signup = get_engine()  # ← 여기서도 지역 변수명 다르게
                    ok = create_user(eng_signup, new_email, new_pw)
                    if ok:
                        st.success("회원가입 완료! 이제 로그인 탭에서 로그인하세요.") 
                    else: 
                        st.warning("이미 가입된 이메일입니다.")

        # ---------------- 로그인 ----------------
        with tab_login:
            st.subheader("로그인")
            with st.form("login_form"):
                email = st.text_input("이메일", placeholder="you@example.com", key="login_email")
                pw = st.text_input("비밀번호", type="password", key="login_pw")
                remember = st.checkbox("로그인 유지 (브라우저 세션 동안)")
                submitted_login = st.form_submit_button("로그인")

            if submitted_login:
                # 폼에서는 실제 인증 X → 플래그만 세팅 후 즉시 rerun
                st.session_state["_pending_login"] = {"email": email, "pw": pw}
                st.rerun()


if __name__ == "__main__":
    main() 

# with tab_profile:
#     st.subheader("내 정보")
#     if not is_logged_in():
#         st.warning("로그인이 필요합니다.")
#     else:
#         auth = st.session_state["auth"]
#         st.write(f"**이메일**: {auth['email']}")
#         # 실제 서비스라면 여기에 프로필 수정, 비밀번호 변경 로직 등을 추가
#         if st.button("로그아웃", key="logout_btn2"):
#             logout_user()
#             st.success("로그아웃 되었습니다.")
#             st.rerun()
