# core/openclip_model.py
import torch
import open_clip
import streamlit as st

device = "cuda" if torch.cuda.is_available() else "cpu"

@st.cache_resource(show_spinner="📍 장소를 추천 중입니다... 조금만 기다려 주세요!")
def load_openclip_model():
    model, _, preprocess = open_clip.create_model_and_transforms(
        "ViT-g-14",
        pretrained="laion2b_s12b_b42k"
    )
    tokenizer = open_clip.get_tokenizer("ViT-g-14")
    model.to(device).eval()
    return model, preprocess, tokenizer, device
