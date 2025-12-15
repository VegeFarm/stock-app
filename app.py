import streamlit as st
import pandas as pd
import json

st.set_page_config(page_title="1행 & G열 보기", layout="wide")
st.title("엑셀 캡처 기준: 1행(헤더) + G열(품목명) 웹에서 보기")

# ✅ 1행(헤더) - 사진 기준으로 임시 고정
headers = [
    ("G", "품목"),
    ("H", "재고"),
    ("I", "입고"),
    ("J", "보유수량"),
    ("K", "1차"),
    ("L", "2차"),
    ("M", "3차"),
    ("N", "주문수량"),
    ("P", "남은수량"),
]

# ✅ G열(품목명) - 사진 기준으로 임시 고정
g_items = [
    "고수", "뉴그린", "딜", "적환", "로케트", "로즈잎", "바질", "비타민",
    "쌈추", "쌈샐러리", "와일드", "잎로메인", "적겨자", "적근대", "적치커리",
    "청경채", "치커리", "케일", "통로메인", "향나물", "당귀"
]

st.caption("※ 아직 사진 업로드/OCR은 구현하지 않고, 위 캡처에서 읽은 값만 화면에 띄웁니다.")

col1, col2 = st.columns(2)

with col1:
    st.subheader("1행(헤더) 보기/수정")
    df_headers = pd.DataFrame(headers, columns=["열", "헤더"])
    df_headers = st.data_editor(df_headers, use_container_width=True, key="headers_editor")

    st.download_button(
        "헤더 CSV 다운로드",
        data=df_headers.to_csv(index=False).encode("utf-8-sig"),
        file_name="headers.csv",
        mime="text/csv",
        key="dl_headers",
    )

with col2:
    st.subheader("G열(품목명) 보기/수정")
    df_g = pd.DataFrame({"G열_품목명": g_items})
    df_g = st.data_editor(df_g, use_container_width=True, num_rows="dynamic", key="gcol_editor")

    st.download_button(
        "G열 품목명 CSV 다운로드",
        data=df_g.to_csv(index=False).encode("utf-8-sig"),
        file_name="g_column_items.csv",
        mime="text/csv",
        key="dl_gcol",
    )

st.divider()
st.subheader("한 번에 JSON으로도 내보내기")

payload = {
    "headers": df_headers.to_dict(orient="records"),
    "g_column_items": df_g["G열_품목명"].dropna().astype(str).tolist(),
}

st.download_button(
    "JSON 다운로드",
    data=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    file_name="header_and_gcol.json",
    mime="application/json",
    key="dl_json",
)
