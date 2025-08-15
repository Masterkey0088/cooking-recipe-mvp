# Streamlit Cooking Recipe MVP — rewritten clean version (images OFF)
# ---------------------------------------------------------------
# Drop this file in place of your current `streamlit_app.py`.
#  - Clean structure
#  - Image UI/logic removed (can be toggled later)
#  - Ingredients: de-dup quantities, avoid "適量" when possible, show as "材料名 量"
#  - Steps: normalize to "STEP n" and strip existing prefixes
#  - Tools: auto-infer from ingredients/steps when empty
#  - Robust OpenAI call with JSON parsing + safe fallback
# ---------------------------------------------------------------

from __future__ import annotations
import os
import re
import json
from typing import List, Optional

import streamlit as st
from pydantic import BaseModel, Field, ValidationError

# ---------- Page config ----------
st.set_page_config(page_title="晩ごはん一撃レコメンド", layout="wide")

# ---------- (Optional) Access gate ----------
ACCESS_CODE = st.secrets.get("APP_ACCESS_CODE") or os.getenv("APP_ACCESS_CODE")
if ACCESS_CODE:
    if not st.session_state.get("auth_ok"):
        st.title("🔒 アクセスコードが必要です")
        code = st.text_input("アクセスコード", type="password")
        if st.button("Enter"):
            if code == ACCESS_CODE:
                st.session_state["auth_ok"] = True
                st.rerun()
            else:
                st.error("アクセスコードが違います")
        st.stop()

# ==============================================================
# Models
# ==============================================================
class Ingredient(BaseModel):
    name: str
    amount: Optional[str] = None
    is_optional: bool = False
    substitution: Optional[str] = None

class Step(BaseModel):
    text: str

class Recipe(BaseModel):
    recipe_title: str = Field(..., alias="title")
    servings: int = 2
    total_time_min: Optional[int] = None
    difficulty: Optional[str] = None
    ingredients: List[Ingredient]
    steps: List[Step]
    equipment: Optional[List[str]] = None

class RecipeSet(BaseModel):
    recommendations: List[Recipe]

# ==============================================================
# Utilities — steps formatting
# ==============================================================
_STEP_PREFIX_RE = re.compile(
    r"^\s*(?:STEP\s*[0-9０-９]+[:：\-\s]*|[0-9０-９]+[\.．、\)）]\s*|[①-⑳]\s*)"
)

def strip_step_prefix(text: str) -> str:
    return _STEP_PREFIX_RE.sub('', text or '').strip()

# ==============================================================
# Utilities — ingredient quantity normalization
# ==============================================================
TSP_IN_TBSP = 3.0

PROTEIN_G_PER_SERV = {
    "鶏むね肉": 100, "鶏もも肉": 100, "豚肉": 100, "牛肉": 100, "ひき肉": 100,
    "鮭": 90, "さば": 90, "ツナ": 70, "ベーコン": 30, "ハム": 30, "豆腐": 150
}
VEG_G_PER_SERV = {
    "玉ねぎ": 50, "ねぎ": 10, "長ねぎ": 20, "キャベツ": 80, "にんじん": 40,
    "じゃがいも": 80, "なす": 60, "ピーマン": 40, "もやし": 100, "ブロッコリー": 70,
    "きのこ": 60, "しめじ": 60, "えのき": 60, "トマト": 80, "青菜": 70, "小松菜": 70, "ほうれん草": 70
}
COND_TSP_PER_SERV = {
    "塩": 0.125, "砂糖": 0.5, "しょうゆ": 1.0, "醤油": 1.0, "みりん": 1.0, "酒": 1.0,
    "酢": 1.0, "コチュジャン": 0.5, "味噌": 1.5, "味の素": 0.25, "顆粒だし": 0.5
}
OIL_TSP_PER_SERV = {"サラダ油": 1.0, "ごま油": 0.5, "オリーブオイル": 1.0}
PIECE_PER_SERV = {"卵": "1個", "にんにく": "0.5片", "生姜": "0.5片"}

_num_re = re.compile(r'([0-9]+(?:\.[0-9]+)?)')

def _has_number(s: str) -> bool:
    return bool(_num_re.search(s or ""))

def _round_tsp_to_pretty(tsp: float) -> str:
    if tsp <= 0.15:
        return "少々"
    tbsp = tsp / TSP_IN_TBSP
    if tbsp >= 1.0:
        val = round(tbsp * 2) / 2
        return f"大さじ{val:g}"
    else:
        val = round(tsp * 2) / 2
        return f"小さじ{val:g}"

def _grams_to_pretty(g: int) -> str:
    if g < 60:
        step = 10
    elif g < 150:
        step = 25
    else:
        step = 50
    pretty = int(round(g / step) * step)
    return f"{pretty}g"

def _guess_amount(name: str, servings: int) -> str:
    for key, per in PIECE_PER_SERV.items():
        if key in name:
            m = _num_re.search(per)
            num = float(m.group(1)) if m else 1.0
            unit = per.replace(str(num).rstrip('0').rstrip('.'), '')
            total = num * servings
            if abs(total - int(total)) < 1e-6:
                return f"{int(total)}{unit}"
            return f"{total:g}{unit}"
    for key, g in PROTEIN_G_PER_SERV.items():
        if key in name:
            return _grams_to_pretty(int(g * servings))
    for key, g in VEG_G_PER_SERV.items():
        if key in name:
            return _grams_to_pretty(int(g * servings))
    for key, tsp in OIL_TSP_PER_SERV.items():
        if key in name:
            return _round_tsp_to_pretty(tsp * servings)
    for key, tsp in COND_TSP_PER_SERV.items():
        if key in name:
            return _round_tsp_to_pretty(tsp * servings)
    if any(k in name for k in ["胡椒", "こしょう", "黒胡椒", "一味", "七味", "ラー油"]):
        return "少々"
    return "適量"

# Quantity inside name, e.g., "200g 豚肉" or "にんにく 1片"
_QTY_IN_NAME_RE = re.compile(
    r'(?:^|\s)('
    r'(?:小さじ|大さじ)\s*\d+(?:\.\d+)?'
    r'|(?:\d+(?:\.\d+)?)\s*(?:g|グラム|kg|㎏|ml|mL|L|cc|カップ|cup|個|片|枚|本)'
    r'|少々|適量'
    r')(?=\s|$)'
)

def split_quantity_from_name(name: str) -> tuple[str, Optional[str]]:
    txt = name or ""
    m = _QTY_IN_NAME_RE.search(txt)
    qty = m.group(1) if m else None
    base = _QTY_IN_NAME_RE.sub(" ", txt).strip()
    base = re.sub(r'\s{2,}', ' ', base)
    return (base or txt), qty

def sanitize_amount(amount: Optional[str]) -> Optional[str]:
    if not amount:
        return None
    a = amount.strip().replace("．", ".").replace(".0", "")
    if a in {"小さじ0", "大さじ0", "0g", "0個", "0片", "0枚", "0本", "0cc"}:
        return "少々"
    return a

def normalize_ingredients(ings: List[Ingredient], servings: int) -> List[Ingredient]:
    fixed: List[Ingredient] = []
    for it in ings:
        base_name, qty_in_name = split_quantity_from_name(it.name)
        amt = sanitize_amount(getattr(it, "amount", None)) or qty_in_name or ""
        if (not amt) or ("適量" in amt) or (not _has_number(amt) and "少々" not in amt):
            amt = _guess_amount(base_name, servings)
        amt = sanitize_amount(amt) or "適量"
        fixed.append(Ingredient(
            name=base_name,
            amount=amt,
            is_optional=getattr(it, "is_optional", False),
            substitution=getattr(it, "substitution", None),
        ))
    return fixed

# ==============================================================
# Utilities — tools inference
# ==============================================================
_TOOL_RULES = [
    (r"(切る|刻む|みじん|千切り|輪切り|そぎ切り)", ["包丁", "まな板"]),
    (r"(混ぜ|和え|ほぐし|溶き卵|衣を作る)", ["ボウル", "菜箸"]),
    (r"(炒め|焼き色|ソテー|香りが立つまで)", ["フライパン", "フライ返し"]),
    (r"(焼く|トースト|グリル)", ["オーブン/トースター", "天板（アルミホイル）"]),
    (r"(茹で|ゆで|湯が|下茹で)", ["鍋（湯用）", "ザル"]),
    (r"(煮|煮込|煮立|弱火で|中火で|沸騰)", ["鍋", "菜箸"]),
    (r"(蒸し|蒸気|蒸し器)", ["蒸し器（または鍋＋蒸し台）", "蓋"]),
    (r"(揚げ|素揚げ|油で)", ["鍋（揚げ物用）", "油温計", "網じゃくし"]),
    (r"(電子レンジ|レンジ|600W|500W)", ["電子レンジ", "耐熱容器", "ラップ"]),
    (r"(炊く|ご飯|米を研ぐ|炊飯)", ["炊飯器", "ボウル（米研ぎ）"]),
    (r"(皮をむく|すりおろ|おろし)", ["ピーラー/おろし金"]),
    (r"(こす|濾す|漉す)", ["こし器（またはザル）"]),
]
_MEASURE_RE = re.compile(r"(小さじ|大さじ|カップ|cup|cc|ml|mL|L|ℓ)")

def infer_tools_from_text(ingredients_text: str, steps_text: str) -> List[str]:
    txt = f"{ingredients_text}\n{steps_text}"
    tools: List[str] = []
    for pattern, add_list in _TOOL_RULES:
        if re.search(pattern, txt):
            for t in add_list:
                if t not in tools:
                    tools.append(t)
    if _MEASURE_RE.search(txt):
        for t in ["計量スプーン", "計量カップ"]:
            if t not in tools:
                tools.append(t)
    if not tools:
        tools = ["包丁", "まな板", "ボウル", "フライパンまたは鍋", "計量スプーン"]
    return tools

def infer_tools_from_recipe(rec: Recipe) -> List[str]:
    ings_txt = "、".join([i.name for i in rec.ingredients])
    steps_txt = "。".join([s.text for s in rec.steps])
    return infer_tools_from_text(ings_txt, steps_txt)

# ==============================================================
# OpenAI — generation (robust JSON with fallback)
# ==============================================================
USE_OPENAI = True  # OFF でも動くように簡易レシピへフォールバック

from openai import OpenAI
_client: Optional[OpenAI] = None
if USE_OPENAI and (os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY")):
    _client = OpenAI()

PROMPT_TMPL = (
    "You are a helpful Japanese cooking assistant.\n"
    "Given ingredients, servings, theme, genre and max time, propose 1-3 Japanese recipes.\n"
    "Output strict JSON matching this schema in UTF-8 (no markdown).\n"
    "{\n"
    "  \"recommendations\": [\n"
    "    {\n"
    "      \"title\": string,\n"
    "      \"servings\": int,\n"
    "      \"total_time_min\": int,\n"
    "      \"difficulty\": string,\n"
    "      \"ingredients\": [ {\n"
    "        \"name\": string,\n"
    "        \"amount\": string | null,\n"
    "        \"is_optional\": boolean,\n"
    "        \"substitution\": string | null\n"
    "      } ],\n"
    "      \"steps\": [ { \"text\": string } ],\n"
    "      \"equipment\": string[] | null\n"
    "    }\n"
    "  ]\n"
    "}\n"
    "Notes: Amounts should avoid vague words like '適量' when possible; prefer grams, tsp/tbsp."
)

def generate_recipes(ingredients: List[str], servings: int, theme: str, genre: str, max_minutes: int) -> RecipeSet:
    # LLM path
    if _client is not None:
        try:
            sys = "あなたは日本語で回答する料理アシスタントです。"
            usr = (
                f"食材: {', '.join(ingredients) if ingredients else '（未指定）'}\n"
                f"人数: {servings}人\n"
                f"テーマ: {theme}\nジャンル: {genre}\n"
                f"最大調理時間: {max_minutes}分\n"
                "JSONのみを返して下さい。"
            )
            resp = _client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=0.6,
                messages=[
                    {"role": "system", "content": PROMPT_TMPL},
                    {"role": "user", "content": usr},
                ],
            )
            text = resp.choices[0].message.content or "{}"
            data = json.loads(text)
            parsed = RecipeSet.model_validate(data)
            return parsed
        except Exception as e:
            st.info(f"LLMの構造化生成に失敗したためフォールバックします: {e}")

    # Fallback path — simple heuristic one-recipe
    base_ings = [Ingredient(name=x) for x in ingredients[:6]] or [Ingredient(name="鶏むね肉"), Ingredient(name="キャベツ")]
    steps = [
        Step(text="材料を食べやすい大きさに切る"),
        Step(text="フライパンで油を熱し、肉と野菜を炒める"),
        Step(text="しょうゆ・みりん・酒で味付けして全体を絡める"),
    ]
    rec = Recipe(
        title=f"かんたん炒め（{genre}風）", servings=servings, total_time_min=min(20, max_minutes),
        difficulty="かんたん", ingredients=base_ings, steps=steps, equipment=None
    )
    return RecipeSet(recommendations=[rec])

# ==============================================================
# UI — Header & Form (images OFF)
# ==============================================================
st.title("🍳 晩ごはん一撃レコメンド")

with st.form("inputs", clear_on_submit=False, border=True):
    st.text_input("冷蔵庫の食材（カンマ区切り）", key="ingredients", placeholder="例）豚肉, キャベツ, ねぎ")
    c1, c2, c3 = st.columns([1, 1, 1])
    with c1:
        st.slider("人数", 1, 6, 2, 1, key="servings")
    with c2:
        st.selectbox("テーマ", ["時短", "節約", "栄養重視", "子ども向け", "おもてなし"], index=1, key="theme")
    with c3:
        st.selectbox("ジャンル", ["和風", "洋風", "中華風", "韓国風", "エスニック"], index=0, key="genre")
    st.slider("最大調理時間（分）", 5, 90, 30, 5, key="max_minutes")

    # Image settings OFF but keep defaults to avoid NameError downstream
    st.session_state["image_mode"] = "テキストのみ（現在のまま）"
    st.session_state["image_size"] = "1024x1024"
    st.session_state["max_ai_images"] = 0

    submitted = st.form_submit_button("提案を作成", use_container_width=True)

# --------------------------------------------------------------
# Input extraction / pre-processing
# --------------------------------------------------------------
ing_text = st.session_state.get("ingredients", "") or ""
ingredients_raw = [s for s in (t.strip() for t in re.split(r"[、,]", ing_text)) if s]
servings = int(st.session_state.get("servings", 2))
theme = st.session_state.get("theme", "節約")
genre = st.session_state.get("genre", "和風")
max_minutes = int(st.session_state.get("max_minutes", 30))

if not submitted:
    st.stop()

# ==============================================================
# Generate & render
# ==============================================================
try:
    data = generate_recipes(ingredients_raw, servings, theme, genre, max_minutes)
except Exception as e:
    st.error(f"レシピ生成に失敗しました: {e}")
    st.stop()

if not data or not data.recommendations:
    st.warning("候補が作成できませんでした。入力を見直してください。")
    st.stop()

for rec in data.recommendations:
    st.divider()
    st.subheader(rec.recipe_title)

    # Normalize ingredients, infer tools if empty
    rec.ingredients = normalize_ingredients(rec.ingredients, rec.servings)
    tools = rec.equipment or infer_tools_from_recipe(rec)

    colA, colB = st.columns([2, 1])

    with colA:
        # Meta
        meta = []
        meta.append(f"**人数:** {rec.servings}人分")
        if rec.total_time_min:
            meta.append(f"**目安:** {rec.total_time_min}分")
        if rec.difficulty:
            meta.append(f"**難易度:** {rec.difficulty}")
        st.markdown(" / ".join(meta))

        # Tools
        st.markdown("**器具:** " + ("、".join(tools) if tools else "特になし"))

        # Ingredients (name → amount)
        st.markdown("**材料**")
        for i in rec.ingredients:
            base, qty_in_name = split_quantity_from_name(i.name)
            amt = sanitize_amount(getattr(i, "amount", None)) or qty_in_name or "適量"
            st.markdown(
                f"- {base} {amt}"
                + ("（任意）" if i.is_optional else "")
                + (f" / 代替: {i.substitution}" if i.substitution else "")
            )

        # Steps
        st.markdown("**手順**")
        for idx, s in enumerate(rec.steps, 1):
            st.markdown(f"**STEP {idx}**　{strip_step_prefix(s.text)}")

    with colB:
        # Images are OFF in this build
        pass

# End of file
