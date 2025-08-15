import os, io, re, json, textwrap, zipfile, datetime, requests
# --- PATCH A: imports 追加 ---
import base64
from io import BytesIO

from typing import List, Optional, Literal, Tuple

import streamlit as st
from pydantic import BaseModel, Field, ValidationError
from PIL import Image, ImageDraw, ImageFont
from openai import OpenAI
import pandas as pd

import re  # ファイルの先頭付近にあるはずのimport文の近くに追加

def strip_step_prefix(text: str) -> str:
    """先頭の数字やSTEP表記を除去して返す"""
    return re.sub(r'^(STEP\s*\d+[:.\s-]*|\d+[.:、\s-]*)\s*', '', text)

st.set_page_config(page_title="🍳 晩ごはん一撃レコメンド", layout="wide")

SHOW_STEP_IMAGES = False   # 工程写真は表示しない（完成写真のみ表示）

API_KEY = os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY")
if not API_KEY:
    st.error("OPENAI_API_KEY が未設定です。Streamlit Cloud の Secrets に追加してください。")
    st.stop()
os.environ["OPENAI_API_KEY"] = API_KEY

client = OpenAI()
MODEL = "gpt-4o-mini"

FONT_URL = "https://github.com/googlefonts/noto-cjk/raw/main/Sans/OTF/Japanese/NotoSansCJKjp-Regular.otf"
FONT_PATH = "NotoSansCJKjp-Regular.otf"
if not os.path.exists(FONT_PATH):
    try:
        r = requests.get(FONT_URL, timeout=20); r.raise_for_status()
        open(FONT_PATH, "wb").write(r.content)
    except Exception:
        pass

def _load_font(size=28):
    try: return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        try: return ImageFont.truetype("DejaVuSans.ttf", size)
        except Exception: return ImageFont.load_default()

SAFETY_RULES = {
    "chicken": "鶏肉は中心まで十分に加熱（日本基準の目安: 75℃で1分以上相当）。",
    "ground_meat": "挽き肉は中心まで十分に加熱。色変化と肉汁の透明化を確認。",
    "steam_burn": "電子レンジ後は蒸気やけどに注意。ラップは端からゆっくり。",
}
RISKY_KEYWORDS = {
    "chicken": ["鶏","とり","チキン","ささみ","むね肉","もも肉"],
    "ground_meat": ["挽き肉","ひき肉","ミンチ"],
}
def infer_safety_notes(ingredients: List[str]) -> List[str]:
    notes = set()
    for ing in ingredients:
        for key, kws in RISKY_KEYWORDS.items():
            if any(k in ing for k in kws): notes.add(SAFETY_RULES[key])
    return list(notes)

class Ingredient(BaseModel):
    name: str
    amount: str
    is_optional: bool = False
    substitution: Optional[str] = None

class Step(BaseModel):
    n: int
    text: str
    time_min: Optional[int] = None
    image_hint: Optional[str] = None
    safety: Optional[str] = None

class Recipe(BaseModel):
    recipe_title: str
    servings: int
    theme: Optional[str] = None
    genre: Optional[str] = None
    estimated_time_min: Optional[int] = None
    difficulty: Literal["かんたん","ふつう","しっかり"] = "かんたん"
    ingredients: List[Ingredient]
    equipment: List[str]
    steps: List[Step]
    nutrition_estimate: Optional[dict] = None
    leftover_idea: Optional[str] = None
    safety_rules_applied: List[str] = []

class RecipeSet(BaseModel):
    recommendations: List[Recipe] = Field(..., min_items=1, max_items=3)

SYSTEM_PROMPT = (
    "あなたは家庭料理アシスタントです。与えられた食材・人数・テーマ・ジャンルから、"
    "日本の一般家庭向けに再現しやすい晩ごはんレシピを最大3件、JSON構造で提案してください。"
    "電子レンジやフライパン等の一般的な器具を前提に、加熱時間や注意事項を明記します。"
)

def build_user_prompt(ingredients, servings, theme, genre, max_minutes):
    ing_text = ", ".join([i.strip() for i in ingredients if i.strip()]) or "（特になし）"
    return f"""
【条件】
- 人数: {servings} 人分
- 冷蔵庫の食材: {ing_text}
- テーマ: {theme or '指定なし'}
- ジャンル: {genre or '指定なし'}
- 所要時間の目安（最大）: {max_minutes or '指定なし'} 分

【出力要件】
- レシピは最大3件
- 期限が近い/使い切りたい食材を優先（仮定）
- ワンパン/レンジ等で洗い物を減らす工夫
- 1人前のカロリー/たんぱく質の概算（可能な範囲で）
"""

def _extract_minutes(text):
    if not isinstance(text, str): return None
    m = re.search(r"(\d+)\s*分", text); return int(m.group(1)) if m else None

def _adapt_to_schema(obj: dict, servings_default: int, theme: str, genre: str):
    if isinstance(obj, dict) and "recipes" in obj and isinstance(obj["recipes"], list):
        recs = []
        for idx, r in enumerate(obj["recipes"], start=1):
            title = r.get("name") or r.get("title") or f"レシピ{idx}"
            ing_objs = []
            for it in r.get("ingredients", []):
                if isinstance(it, str): ing_objs.append({"name": it, "amount": "適量"})
                elif isinstance(it, dict):
                    ing_objs.append({
                        "name": it.get("name",""), "amount": it.get("amount","適量"),
                        "is_optional": bool(it.get("optional", False)),
                        "substitution": it.get("substitution")
                    })
            steps = []
            for i, s in enumerate(r.get("instructions") or r.get("steps") or [], start=1):
                if isinstance(s, str): steps.append({"n": i, "text": s})
                elif isinstance(s, dict): steps.append({"n": int(s.get("n", i)), "text": s.get("text","")})
            recs.append({
                "recipe_title": title,
                "servings": int(r.get("servings", servings_default)),
                "theme": theme or r.get("theme"),
                "genre": genre or r.get("genre"),
                "estimated_time_min": r.get("estimated_time_min") or _extract_minutes(r.get("cooking_time")),
                "difficulty": r.get("difficulty","かんたん"),
                "ingredients": ing_objs,
                "equipment": r.get("equipment", []),
                "steps": steps,
                "nutrition_estimate": r.get("nutrition_estimate") or r.get("nutrition") or {},
                "leftover_idea": r.get("leftover_idea") or r.get("leftover"),
                "safety_rules_applied": []
            })
        return {"recommendations": recs}
    return None

def call_openai_for_recipes(ingredients, servings, theme, genre, max_minutes) -> RecipeSet:
    user_prompt = build_user_prompt(ingredients, servings, theme, genre, max_minutes)
    completion = client.chat.completions.create(
        model=MODEL, temperature=0.3,
        messages=[{"role":"system","content":SYSTEM_PROMPT},{"role":"user","content":user_prompt}],
        response_format={"type":"json_object"},
    )
    content = completion.choices[0].message.content

    try:
        data = RecipeSet.model_validate_json(content)
    except ValidationError:
        import json as _json
        try: obj = _json.loads(content)
        except Exception as e: raise RuntimeError(f"JSON parse error: {e}\nRaw: {content[:450]}")
        adapted = _adapt_to_schema(obj, servings, theme, genre)
        if not adapted: raise RuntimeError(f"Unexpected JSON shape.\nRaw: {content[:450]}")
        data = RecipeSet.model_validate(adapted)

    extra = infer_safety_notes(ingredients)
    for r in data.recommendations:
        r.safety_rules_applied = list(set((r.safety_rules_applied or []) + extra))
        if max_minutes and (r.estimated_time_min or 0) > max_minutes:
            r.recipe_title += "（時間オーバー気味）"
    return data

def _wrap_by_chars(s: str, width: int) -> str:
    s = s.replace("\n"," "); import textwrap as _tw
    return "\n".join(_tw.wrap(s, width=28))

def make_step_image(step, w=900, h=600, bg=(248,248,248)):
    img = Image.new("RGB",(w,h),bg); draw = ImageDraw.Draw(img)
    title_font=_load_font(40); text_font=_load_font(30); warn_font=_load_font(26)
    pad=32
    draw.text((pad,pad), f"STEP {step.n}", font=title_font, fill=(0,0,0))
    body=_wrap_by_chars(step.text,28)
    draw.multiline_text((pad,pad+70), body, font=text_font, fill=(25,25,25), spacing=8)
    if getattr(step,"safety",None):
        warn=_wrap_by_chars("⚠ "+step.safety,30)
        draw.multiline_text((pad,h-90), warn, font=warn_font, fill=(180,0,0), spacing=4)
    draw.rectangle([0,0,w-1,h-1], outline=(210,210,210), width=1)
    return img

from math import sqrt

# 料理アクションと道具の簡易マップ（必要に応じて増やせます）
ACTION_MAP = {
    "切る": "chopping", "ざく切り": "rough chop", "みじん": "minced",
    "炒め": "stir fry", "焼く": "grilling", "茹で": "boiling",
    "煮": "simmering", "蒸": "steaming", "和える": "mixing", "混ぜ": "mixing",
    "レンジ": "microwave", "電子レンジ": "microwave"
}
TOOL_HINTS = {
    "切る": "knife, cutting board", "みじん": "knife, cutting board",
    "炒め": "frying pan, spatula", "焼く": "frying pan",
    "茹で": "pot of boiling water", "煮": "saucepan",
    "蒸": "steamer pot with lid", "和える": "mixing bowl", "混ぜ": "mixing bowl",
    "レンジ": "microwave oven", "電子レンジ": "microwave oven"
}
# よく使う食材の簡易日英マップ（足りなければ随時足せます）
ING_EN = {
    "キャベツ":"cabbage","ねぎ":"green onion","長ねぎ":"leek","玉ねぎ":"onion",
    "鶏むね肉":"chicken breast","鶏もも肉":"chicken thigh","豚肉":"pork","牛肉":"beef",
    "なす":"eggplant","ピーマン":"bell pepper","もやし":"bean sprouts","きのこ":"mushroom",
    "しめじ":"shimeji mushroom","えのき":"enoki mushroom","豆腐":"tofu","卵":"egg",
    "ご飯":"rice","米":"rice","うどん":"udon noodles","そば":"soba noodles","パスタ":"pasta",
    "にんじん":"carrot","じゃがいも":"potato","ブロッコリー":"broccoli","トマト":"tomato",
    "鮭":"salmon","さば":"mackerel","ツナ":"tuna","ベーコン":"bacon","ハム":"ham","チーズ":"cheese"
}

def _jp_ing_to_en(word: str) -> str | None:
    for jp, en in ING_EN.items():
        if jp in word:
            return en
    return None

def _build_step_keywords(step_text: str, ingredients_jp: list[str]) -> str:
    # 料理アクション/道具ヒント
    act = tool = None
    for jp, en in ACTION_MAP.items():
        if jp in step_text:
            act, tool = en, TOOL_HINTS.get(jp)
            break
    # 食材（手順文 or 全体の材料から拾う）
    ing_en = None
    for jp, en in ING_EN.items():
        if jp in step_text:
            ing_en = en; break
    if not ing_en:
        for it in ingredients_jp:
            ing_en = _jp_ing_to_en(it)
            if ing_en: break

    # 最終クエリ（顔NG・手元・キッチン等の意図も追加）
    kws = [k for k in [act, ing_en, tool, "cooking", "kitchen", "hands", "close-up", "no face"] if k]
    return " ".join(kws) if kws else f"{step_text} cooking kitchen hands"

def _cosine(a: list[float], b: list[float]) -> float:
    na = sqrt(sum(x*x for x in a)); nb = sqrt(sum(y*y for y in b))
    if na == 0 or nb == 0: return 0.0
    return sum(x*y for x, y in zip(a, b)) / (na * nb)

@st.cache_data(show_spinner=False)
def _embed(text: str) -> list[float] | None:
    try:
        return client.embeddings.create(model="text-embedding-3-small", input=text).data[0].embedding
    except Exception as e:
        st.session_state.setdefault("img_errors", []).append(f"embed_err: {e}")
        return None

@st.cache_data(show_spinner=False)
def _pexels_search_json(query: str, per_page: int = 18, orientation: str = "landscape") -> dict:
    key = os.getenv("PEXELS_API_KEY") or st.secrets.get("PEXELS_API_KEY")
    if not key:
        st.session_state.setdefault("img_errors", []).append("PEXELS_API_KEY が未設定です（Secretsに追加）")
        return {}
    r = requests.get(
        "https://api.pexels.com/v1/search",
        headers={"Authorization": key},
        params={"query": query, "per_page": per_page, "orientation": orientation}
    )
    try:
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


# 右カラム描画（画像まわりをまとめた関数）
def render_right_column(rec,
                        image_mode="テキストのみ（現在のまま）",
                        image_size="1024x1024"):
    """右側カラム：完成写真のみ表示"""

    # --- AI画像（OpenAI: gpt-image-1） ---
    if image_mode.startswith("AI画像"):
        hero_bytes = _openai_image_bytes(
            _dish_prompt(rec.recipe_title, [i.name for i in rec.ingredients]),
            size=image_size
        )
        if hero_bytes:
            st.image(Image.open(BytesIO(hero_bytes)),
                     caption="完成イメージ", use_container_width=True)
        return

    # --- 素材写真（Pexels） ---
    if image_mode.startswith("素材写真"):
        hero_bytes = _stock_dish_image(rec.recipe_title, [i.name for i in rec.ingredients])
        if hero_bytes:
            st.image(Image.open(BytesIO(hero_bytes)),
                     caption="完成イメージ（Photos: Pexels）", use_container_width=True)
        return

    # --- 画像なし（テキストのみ） ---
    # 何も表示しない（左側に手順テキストは出ています）
    return

# --- Pexels 画像取得ユーティリティ ---
def _pexels_search(query: str, per_page: int = 1, orientation: str = "landscape") -> list[str]:
    key = os.getenv("PEXELS_API_KEY") or st.secrets.get("PEXELS_API_KEY", None)
    if not key:
        st.session_state.setdefault("img_errors", []).append("PEXELS_API_KEY が未設定です（Secretsに追加してください）")
        return []
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": key},
            params={"query": query, "per_page": per_page, "orientation": orientation}
        )
        if r.status_code != 200:
            st.session_state.setdefault("img_errors", []).append(f"Pexels {r.status_code}: {r.text[:160]}")
            return []
        data = r.json()
        return [p["src"]["large"] for p in data.get("photos", [])]
    except Exception as e:
        st.session_state.setdefault("img_errors", []).append(str(e))
        return []

def _fetch_image_bytes(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=20); r.raise_for_status()
        return r.content
    except Exception as e:
        st.session_state.setdefault("img_errors", []).append(str(e))
        return None

def _stock_dish_image(recipe_title: str, ingredients: list[str]) -> bytes | None:
    q = f"{recipe_title} {', '.join(ingredients[:3])} dish food"
    urls = _pexels_search(q, per_page=1)
    return _fetch_image_bytes(urls[0]) if urls else None

def _stock_step_image(step_text: str, ingredients_jp: list[str]) -> bytes | None:
    # 1) 日本語→英キーワード化
    query = _build_step_keywords(step_text, ingredients_jp)

    # 2) 候補を広めに取得
    data = _pexels_search_json(query, per_page=18)
    photos = data.get("photos", [])
    if not photos:
        return None

    # 3) altテキストで再ランク（埋め込み類似度）
    qv = _embed(query)
    if not qv:
        url = photos[0]["src"]["large"]  # 埋め込み失敗時は先頭を採用
        return _fetch_image_bytes(url)

    scored = []
    for p in photos:
        alt = p.get("alt") or ""
        av = _embed(alt) or qv
        scored.append(( _cosine(qv, av), p ))
    scored.sort(reverse=True, key=lambda x: x[0])

    top = scored[0][1]
    return _fetch_image_bytes(top["src"]["large"])


@st.cache_data(show_spinner=False)
def _openai_image_bytes(prompt: str, size: str = "1024x1024", model: str = "gpt-image-1") -> bytes | None:
    """OpenAI画像APIでPNGバイト列を返す（同一プロンプトはキャッシュ）"""
    allowed = {"256x256","512x512","1024x1024","1792x1024","1024x1792"}
    if size not in allowed:
        size = "1024x1024"
    try:
        resp = client.images.generate(model=model, prompt=prompt, size=size)
        b64 = resp.data[0].b64_json
        return base64.b64decode(b64)
    except Exception as e:
        st.session_state.setdefault("img_errors", []).append(str(e))
        return None

def _overlay_caption(png_bytes: bytes, caption: str) -> Image.Image:
    """画像下部に半透明帯＋白文字キャプション"""
    im = Image.open(BytesIO(png_bytes)).convert("RGBA")
    w, h = im.size
    overlay = Image.new("RGBA", im.size, (0,0,0,0))
    draw = ImageDraw.Draw(overlay)
    band_h = max(80, int(h*0.18))
    draw.rectangle([0, h-band_h, w, h], fill=(0,0,0,140))
    font = _load_font(28)
    import textwrap
    wrapped = "\n".join(textwrap.wrap(caption.replace("\n"," "), width=28))
    pad = 18
    draw.multiline_text((pad, h-band_h+pad), wrapped, font=font, fill=(255,255,255,230), spacing=6)
    return Image.alpha_composite(im, overlay).convert("RGB")

def _dish_prompt(recipe_title: str, ingredients: list[str]) -> str:
    ing = ", ".join(ingredients[:5])
    return (
        f"完成した料理の写真。料理名: {recipe_title}。主な食材: { ing }。"
        "日本の家庭料理の盛り付け、自然光、木のテーブル、被写界深度浅め。"
        "人物の顔・ロゴは映さない。リアル写真風、彩度はやや控えめ。"
    )

def _step_prompt(step_text: str) -> str:
    return (
        f"家庭のキッチンでの調理過程の手元写真。内容: {step_text}。"
        "まな板やフライパンなどを手元アップで。人物の顔・ブランドロゴは映さない。"
        "自然光、清潔感、リアル写真風。"
    )


def _dish_prompt(recipe_title: str, ingredients: list[str]) -> str:
    ing = ", ".join(ingredients[:5])
    return (
        f"完成した料理の写真。料理名: {recipe_title}。主な食材: {ing}。"
        "日本の家庭料理の盛り付け、自然光、木のテーブル、被写界深度浅め。"
        "人物の顔・ロゴは映さない。リアル写真風、彩度はやや控えめ。"
    )

def _step_prompt(step_text: str) -> str:
    return (
        f"家庭のキッチンでの調理過程の手元写真。内容: {step_text}。"
        "まな板やフライパンなどを手元アップで。人物の顔・ブランドロゴは映さない。"
        "自然光、清潔感、リアル写真風。"
    )

def recipes_to_dataframes(data: RecipeSet) -> Tuple[pd.DataFrame,pd.DataFrame,pd.DataFrame]:
    rec_rows=[{"recipe_title": r.recipe_title, "servings": r.servings,
               "theme": r.theme, "genre": r.genre,
               "estimated_time_min": r.estimated_time_min, "difficulty": r.difficulty}
              for r in data.recommendations]
    df_recipes = pd.DataFrame(rec_rows)

    ing_rows=[]
    for r in data.recommendations:
        for i in r.ingredients:
            ing_rows.append({"recipe_title": r.recipe_title, "name": i.name, "amount": i.amount,
                             "is_optional": i.is_optional, "substitution": i.substitution})
    df_ingredients = pd.DataFrame(ing_rows)

    step_rows=[]
    for r in data.recommendations:
        for s in r.steps:
            step_rows.append({"recipe_title": r.recipe_title, "n": s.n, "text": s.text,
                              "time_min": s.time_min, "image_hint": s.image_hint, "safety": s.safety})
    df_steps = pd.DataFrame(step_rows)
    return df_recipes, df_ingredients, df_steps

def build_zip_bytes(data: RecipeSet) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        df_recipes, df_ingredients, df_steps = recipes_to_dataframes(data)
        zf.writestr("recipes.csv", df_recipes.to_csv(index=False))
        zf.writestr("ingredients.csv", df_ingredients.to_csv(index=False))
        zf.writestr("steps.csv", df_steps.to_csv(index=False))
        md_all=[]
        for r in data.recommendations:
            md = [f"# {r.recipe_title}",
                  f"- 人数: {r.servings} / 目安: {r.estimated_time_min or '-'}分 / 難易度: {r.difficulty}",
                  f"- 器具: {', '.join(r.equipment)}",
                  f"- 安全注記: {', '.join(r.safety_rules_applied or []) or '-'}",
                  "", "## 材料"]
            for i in r.ingredients:
                md.append(f"- {i.amount} {i.name}"
                          + ("（任意）" if i.is_optional else "")
                          + (f" / 代替: {i.substitution}" if i.substitution else ""))
            md += ["", "## 手順"]
            for s in r.steps: md.append(f"{s.n}. {s.text}")
            if r.leftover_idea: md += ["", "## 余りの活用", r.leftover_idea]
            md_all.append("\n".join(md))
        zf.writestr("recipes.md", "\n\n---\n\n".join(md_all))
        for idx, r in enumerate(data.recommendations, start=1):
            for s in r.steps[:6]:
                im = make_step_image(s); b = io.BytesIO(); im.save(b, format="PNG")
                zf.writestr(f"images/{idx:02d}_{r.recipe_title}_step{s.n}.png", b.getvalue())
    return buf.getvalue()

st.title("🍳 晩ごはん一撃レコメンド（Streamlit版）")
with st.form("inputs", clear_on_submit=False):
    ing = st.text_input("冷蔵庫の食材（カンマ区切り）", "鶏むね肉, キャベツ, ねぎ")
    col1, col2, col3 = st.columns([1,1,1])
    with col1:
        servings = st.slider("人数", 1, 6, 3, 1)
    with col2:
        theme = st.selectbox("テーマ", ["", "時短", "節約", "子ども向け", "ヘルシー"], index=1)
    with col3:
        genre = st.selectbox("ジャンル", ["", "和風", "洋風", "中華", "韓国風", "エスニック"], index=1)
    max_minutes = st.slider("最大調理時間（分）", 10, 60, 30, 5)

    # 画像設定（新規）
    img_col1, img_col2 = st.columns([2, 1])

    with img_col1:
        image_mode = st.selectbox(
            "画像タイプ",
            ["テキストのみ（現在のまま）", "素材写真（Pexels）", "AI画像（生成）"],
            index=0
        )

    with img_col2:
        image_size = st.selectbox(
            "画像サイズ",
            ["1024x1024", "1792x1024", "1024x1792", "512x512"],
            index=0
        )

    submitted = st.form_submit_button("提案を作成", use_container_width=True)

# --- PATCH C: フォームに項目追加 ---
img_col1, img_col2 = st.columns([2,1])
with img_col1:
    image_mode = st.selectbox(
    "画像タイプ",
    ["テキストのみ（現在のまま）", "AI画像（生成）", "素材写真（Pexels）"],  # ← 追加
    index=0
)

with img_col2:
    max_ai_images = st.slider("レシピあたりのAI画像枚数（ステップ）", 1, 6, 4, 1)
# 画像サイズはお好みで。大きいほど遅くコスト高。
image_size = "768x512"


# --- PATCH D: 送信後のみ結果を表示 ---
# --- replace from here to the end of the results section ---
if submitted:
    ingredients = [x.strip() for x in ing.split(",") if x.strip()]
    try:
        data = call_openai_for_recipes(ingredients, int(servings), theme, genre, int(max_minutes))
    except Exception as e:
        st.warning(f"LLMエラー。フォールバックに切替: {e}")
        # 最低1件は返すフォールバック
        rec = Recipe(
            recipe_title="鶏むねともやしの塩炒め",
            servings=int(servings),
            theme=theme or "時短", genre=genre or "和風",
            estimated_time_min=15, difficulty="かんたん",
            ingredients=[Ingredient(name=i, amount="適量") for i in (ingredients[:5] or ["鶏むね肉","もやし","塩","油","こしょう"])],
            equipment=["フライパン","ボウル"],
            steps=[Step(n=1,text="具材を切る。フライパンを中火で温め油を敷く。"),
                   Step(n=2,text="固い野菜→肉/豆腐→柔らかい野菜の順に炒め、塩で調える。"),
                   Step(n=3,text="器に盛る。必要ならごま油少々。")],
            nutrition_estimate={"kcal_per_serving":350,"protein_g":20},
            leftover_idea="翌日はスープや丼にリメイク",
            safety_rules_applied=infer_safety_notes(ingredients),
        )
        data = RecipeSet(recommendations=[rec])

    st.success(f"候補数: {len(data.recommendations)} 件")

    # ダウンロード（CSV/MD/画像）
    zip_bytes = build_zip_bytes(data)
    st.download_button(
        "CSV・Markdown・画像をまとめてダウンロード（ZIP）",
        data=zip_bytes,
        file_name=f"recipes_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
        mime="application/zip",
        use_container_width=True,
    )

    # レシピ表示
    for rec in data.recommendations:
        st.divider()
        st.subheader(rec.recipe_title)

        colA, colB = st.columns([2, 1])

        with colA:
            st.markdown(
                f"- 人数: {rec.servings}人分 / 目安: {rec.estimated_time_min or '-'}分 / 難易度: {rec.difficulty}\n"
                f"- 器具: {', '.join(rec.equipment)}\n"
                f"- 安全注記: {', '.join(rec.safety_rules_applied or []) or '-'}"
            )
            st.markdown("**材料**")
            for i in rec.ingredients:
                st.markdown(
                    f"- {i.amount} {i.name}"
                    + ("（任意）" if i.is_optional else "")
                    + (f" / 代替: {i.substitution}" if i.substitution else "")
                )
            st.markdown("**手順**")
            for idx, s in enumerate(rec.steps, 1):
                st.markdown(f"**STEP {idx}**　{strip_step_prefix(s.text)}")

        with colB:
            # 右カラムは関数を1行で呼ぶだけ（インデント事故を根絶）
            render_right_column(rec, image_mode, image_size)

    # 画像失敗時の簡易ログ（任意）
    err_list = st.session_state.get("img_errors") or []
    if err_list:
        with st.expander("画像取得/生成のエラーメモ", expanded=False):
            st.code(err_list[-1])
        # 次回に持ち越さないようにクリア（存在チェックつき）
        st.session_state["img_errors"] = []




