# -*- coding: utf-8 -*-
# ごはんの神様に相談だ！ / Streamlit App
# 方式A：Secretsの APP_MODE によりベータ/開発を切替
#   - APP_MODE = "beta"  → ベータ版（テストユーザー向け、安定設定）
#   - APP_MODE = "dev"   → 開発版（フィードバック反映の実験設定）
#   - APP_MODE = "prod"  → 本番版
# 必須Secrets: OPENAI_API_KEY（OpenAI使用時）、任意: APP_MODE, APP_ACCESS_CODE

from __future__ import annotations
import os
import re
import json
from typing import List, Optional

import streamlit as st
from pydantic import BaseModel, Field

# ------------------------------------------------------------
# App mode & feature flags（方式A）
# ------------------------------------------------------------
APP_MODE = (st.secrets.get("APP_MODE") or os.getenv("APP_MODE") or "beta").lower()
IS_DEV = APP_MODE in ("dev", "development")
IS_PROD = APP_MODE in ("prod", "production")

APP_TITLE = "ごはんの神様に相談だ！" + ("（開発版）" if IS_DEV else ("（本番）" if IS_PROD else "（ベータ版）"))
st.set_page_config(page_title=APP_TITLE, layout="wide")
st.title(f"🍳 {APP_TITLE}")

FEATURES = {
    # 画像UI（将来ONにしたい時のフラグ）※現状OFF
    "ENABLE_IMAGE_UI": False,

    # 品質フィルタ＋自動リトライ
    "ENABLE_QUALITY_FILTER": True,
    "MAX_QUALITY_RETRY": 3 if not IS_DEV else 5,
    "KEEP_AT_LEAST_ONE": True if not IS_DEV else False,

    # モデル温度（開発版は探索多め）
    "TEMPERATURE": 0.4 if not IS_DEV else 0.6,

    # 開発者向けデバッグ
    "SHOW_DEBUG_PANEL": IS_DEV,
}

# ------------------------------------------------------------
# （任意）アクセスコードロック
# ------------------------------------------------------------
ACCESS_CODE = st.secrets.get("APP_ACCESS_CODE") or os.getenv("APP_ACCESS_CODE")
if ACCESS_CODE:
    if not st.session_state.get("auth_ok"):
        st.info("このアプリはアクセスコードが必要です。")
        code = st.text_input("アクセスコード", type="password")
        if st.button("Enter", use_container_width=True):
            if code == ACCESS_CODE:
                st.session_state["auth_ok"] = True
                st.rerun()
            else:
                st.error("アクセスコードが違います")
                st.stop()
        st.stop()

# ============================================================
# データモデル
# ============================================================
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

# ============================================================
# ユーティリティ：手順整形（STEP n 表記に統一）
# ============================================================
_STEP_PREFIX_RE = re.compile(
    r"^\s*(?:STEP\s*[0-9０-９]+[:：\-\s]*|[0-9０-９]+[\.．、\)）]\s*|[①-⑳]\s*)"
)
def strip_step_prefix(text: str) -> str:
    return _STEP_PREFIX_RE.sub('', text or '').strip()

# ============================================================
# ユーティリティ：材料の分量推定・正規化（「材料名 量」に統一）
# ============================================================
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
        val = round(tbsp * 2) / 2  # 0.5刻み
        return f"大さじ{val:g}"
    else:
        val = round(tsp * 2) / 2
        return f"小さじ{val:g}"

def _grams_to_pretty(g: int) -> str:
    if g < 60: step = 10
    elif g < 150: step = 25
    else: step = 50
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

# 材料名の中に埋まった分量を抽出（200g 豚肉／にんにく 1片 等）
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

# ============================================================
# ユーティリティ：器具推定（材料/手順から）
# ============================================================
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

# ============================================================
# ユーティリティ：品質チェック（✅のみ表示用）
# ============================================================
HEAT_WORDS = ["弱火", "中火", "強火", "沸騰", "余熱", "オーブン", "レンジ"]
SEASONINGS = ["塩", "砂糖", "しょうゆ", "醤油", "みりん", "酒", "味噌", "酢", "ごま油", "オリーブオイル", "バター", "だし"]

def quality_check(rec) -> tuple[bool, List[str]]:
    warns: List[str] = []
    if len(getattr(rec, "ingredients", []) or []) < 3:
        warns.append("材料が少なすぎます（3品以上を推奨）")
    if len(getattr(rec, "steps", []) or []) < 3:
        warns.append("手順が少なすぎます（3ステップ以上を推奨）")

    step_text = "。".join([getattr(s, "text", "") for s in (rec.steps or [])])
    if not any(w in step_text for w in HEAT_WORDS):
        warns.append("火加減や加熱の記述がありません（弱火/中火/強火 や レンジ時間の明示を推奨）")

    ing_txt = "、".join([f"{getattr(i, 'name', '')} {getattr(i, 'amount', '')}" for i in (rec.ingredients or [])])
    if not any(s in ing_txt for s in SEASONINGS):
        warns.append("基本的な調味が見当たりません（塩・しょうゆ・みりん等）")
    if "適量" in ing_txt:
        warns.append("“適量”が含まれています（できるだけ小さじ/大さじ/グラム表記に）")

    ok = (len(warns) == 0)
    return ok, warns

def _filter_passed_recipes(recommendations: List[Recipe]) -> List[Recipe]:
    passed = []
    for r in recommendations:
        ok, _ = quality_check(r)
        if ok:
            passed.append(r)
    return passed

# ============================================================
# 🔥 栄養プロファイル & 概算ロジック（ここから新規追加）
# ============================================================
NUTRI_PROFILES = {
    "ふつう":   {"kcal": (500, 800), "protein_g": (20, 35), "salt_g": (0, 2.5)},
    "ダイエット": {"kcal": (350, 600), "protein_g": (25, 40), "salt_g": (0, 2.0)},
    "がっつり": {"kcal": (700,1000), "protein_g": (35, 55), "salt_g": (0, 3.0)},
    "減塩":     {"kcal": (500, 800), "protein_g": (20, 35), "salt_g": (0, 2.0)},
}

FOODS = {
    # たんぱく源（100g）
    "鶏むね肉": {"kcal":120, "protein_g":23, "fat_g":2,  "carb_g":0,  "salt_g":0},
    "鶏もも肉": {"kcal":200, "protein_g":17, "fat_g":14, "carb_g":0,  "salt_g":0},
    "豚肉":     {"kcal":242, "protein_g":20, "fat_g":19, "carb_g":0,  "salt_g":0},
    "牛肉":     {"kcal":250, "protein_g":20, "fat_g":19, "carb_g":0,  "salt_g":0},
    "ひき肉":   {"kcal":230, "protein_g":19, "fat_g":17, "carb_g":0,  "salt_g":0},
    "鮭":       {"kcal":200, "protein_g":22, "fat_g":12, "carb_g":0,  "salt_g":0},
    "木綿豆腐": {"kcal":72,  "protein_g":7,  "fat_g":4,  "carb_g":2,  "salt_g":0},
    "絹ごし豆腐":{"kcal":56, "protein_g":5,  "fat_g":3,  "carb_g":2,  "salt_g":0},

    # 野菜（100g）
    "キャベツ": {"kcal":23, "protein_g":1, "fat_g":0, "carb_g":5, "salt_g":0},
    "玉ねぎ":   {"kcal":37, "protein_g":1, "fat_g":0, "carb_g":9, "salt_g":0},
    "にんじん": {"kcal":37, "protein_g":1, "fat_g":0, "carb_g":9, "salt_g":0},
    "じゃがいも":{"kcal":76,"protein_g":2, "fat_g":0, "carb_g":17,"salt_g":0},
    "なす":     {"kcal":22, "protein_g":1, "fat_g":0, "carb_g":5, "salt_g":0},
    "もやし":   {"kcal":14, "protein_g":2, "fat_g":0, "carb_g":3, "salt_g":0},

    # 主食（100g）
    "ご飯":     {"kcal":168,"protein_g":2.5,"fat_g":0.3,"carb_g":37,"salt_g":0},

    # 調味料（1大さじ相当）
    "しょうゆ": {"kcal":13, "protein_g":1.4,"fat_g":0,"carb_g":1.2,"salt_g":2.6},
    "みりん":   {"kcal":43, "protein_g":0,"fat_g":0,"carb_g":7.2,"salt_g":0},
    "酒":       {"kcal":11, "protein_g":0,"fat_g":0,"carb_g":0.5,"salt_g":0},
    "砂糖":     {"kcal":35, "protein_g":0,"fat_g":0,"carb_g":9,"salt_g":0},
    "味噌":     {"kcal":33, "protein_g":2,"fat_g":1,"carb_g":4,"salt_g":0.9},
    "ごま油":   {"kcal":111,"protein_g":0,"fat_g":12.6,"carb_g":0,"salt_g":0},
    "オリーブオイル":{"kcal":111,"protein_g":0,"fat_g":12.6,"carb_g":0,"salt_g":0},
    "塩":       {"kcal":0,  "protein_g":0,"fat_g":0,"carb_g":0,"salt_g":6.0}, # 小さじ1=6g → 大さじは×3に注意
}

def amount_to_grams_or_spoons(amount: str) -> tuple[str, float]:
    """
    '200g'→('g',200), '大さじ1'→('tbsp',1), '小さじ2'→('tsp',2), '1個'→('piece',1)
    不明なら ('g', 0) を返す
    """
    if not amount: return ("g", 0.0)
    a = amount.replace("．",".").strip().lower()
    m = re.search(r'(\d+(?:\.\d+)?)\s*(g|グラム)', a)
    if m: return ("g", float(m.group(1)))
    m = re.search(r'大さじ\s*(\d+(?:\.\d+)?)', a)
    if m: return ("tbsp", float(m.group(1)))
    m = re.search(r'小さじ\s*(\d+(?:\.\d+)?)', a)
    if m: return ("tsp", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*個', a)
    if m: return ("piece", float(m.group(1)))
    m = re.search(r'(\d+(?:\.\d+)?)\s*片', a)
    if m: return ("piece", float(m.group(1)) * 0.5)
    return ("g", 0.0)

def tbsp_from_tsp(x: float) -> float: return x / 3.0

def estimate_nutrition(rec) -> dict:
    """食材名の包含マッチでFOODSから拾い、量をg/大さじ/小さじ等から概算。合算→1人前に割る。"""
    total = {"kcal":0.0,"protein_g":0.0,"fat_g":0.0,"carb_g":0.0,"salt_g":0.0}
    for ing in rec.ingredients:
        name = ing.name
        amt_str = ing.amount or ""
        unit, val = amount_to_grams_or_spoons(amt_str)

        key = None
        for k in FOODS.keys():
            if k in name:
                key = k; break
        if not key:
            continue

        base = FOODS[key].copy()
        factor = 0.0
        if unit == "g":
            factor = val / 100.0
        elif unit == "tbsp":
            # FOODSは「1大さじ」基準のものは val をそのまま倍率に
            if key in ["しょうゆ","みりん","酒","味噌","ごま油","オリーブオイル"]:
                factor = val
            else:
                factor = (val * 15.0) / 100.0
        elif unit == "tsp":
            if key in ["しょうゆ","みりん","酒","味噌","ごま油","オリーブオイル"]:
                factor = tbsp_from_tsp(val)
            else:
                factor = (val * 5.0) / 100.0
        elif unit == "piece":
            piece_g = 0
            if "卵" in name: piece_g = 50
            elif "にんにく" in name: piece_g = 5
            else: piece_g = 30
            factor = (piece_g * val) / 100.0
        else:
            continue

        for k in total:
            total[k] += base[k] * factor

    serv = max(1, getattr(rec, "servings", 1))
    for k in total:
        total[k] = round(total[k] / serv, 1)
    return total

def score_against_profile(nutri: dict, profile_name: str) -> dict:
    prof = NUTRI_PROFILES.get(profile_name, NUTRI_PROFILES["ふつう"])
    def mark(val, rng):
        lo, hi = rng
        if val < lo*0.9: return "△"
        if lo <= val <= hi: return "◎"
        if val <= hi*1.15: return "△"
        return "⚠"
    return {
        "kcal":      mark(nutri["kcal"],      prof["kcal"]),
        "protein_g": mark(nutri["protein_g"], prof["protein_g"]),
        "salt_g":    mark(nutri["salt_g"],    prof["salt_g"]),
    }
# ============================================================
# 🔥 栄養ロジック ここまで
# ============================================================

# ============================================================
# OpenAI 呼び出し（JSON生成＋フォールバック）
# ============================================================
USE_OPENAI = True
try:
    from openai import OpenAI
    _client = OpenAI() if (USE_OPENAI and (os.getenv("OPENAI_API_KEY") or st.secrets.get("OPENAI_API_KEY"))) else None
except Exception:
    _client = None

PROMPT_TMPL = (
    "You are a helpful Japanese cooking assistant.\n"
    "Given ingredients, servings, theme, genre and max time, propose 1–3 Japanese home recipes.\n"
    "Output strict JSON matching this schema in UTF-8 (no markdown):\n"
    "{\n"
    '  "recommendations": [\n'
    "    {\n"
    '      "title": string,\n'
    '      "servings": int,\n'
    '      "total_time_min": int,\n'
    '      "difficulty": string,\n'
    '      "ingredients": [ {\n'
    '        "name": string,\n'
    '        "amount": string | null,\n'
    '        "is_optional": boolean,\n'
    '        "substitution": string | null\n'
    "      } ],\n"
    '      "steps": [ { "text": string } ],\n'
    '      "equipment": string[] | null\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "Notes: Avoid vague amounts like '適量' when possible; prefer grams and 大さじ/小さじ."
    " For Japanese home cooking, prefer common ratios where applicable"
    " (e.g., 醤油:みりん:酒 ≈ 1:1:1 for teriyaki; 味噌汁 みそ ≈ 12–18g per 200ml dashi)."
    " Provide cooking times and heat levels (弱火/中火/強火) explicitly. Avoid steps that cannot be executed in a home kitchen.\n"
)

def generate_recipes(
    ingredients: List[str],
    servings: int,
    theme: str,
    genre: str,
    max_minutes: int,
    want_keyword: str = "",
    avoid_keywords: List[str] | None = None
) -> RecipeSet:
    avoid_keywords = avoid_keywords or []

    if _client is not None:
        try:
            avoid_line = ("除外: " + ", ".join(avoid_keywords)) if avoid_keywords else "除外: なし"
            want_line  = ("希望: " + want_keyword) if want_keyword else "希望: なし"
            user_msg = (
                f"食材: {', '.join(ingredients) if ingredients else '（未指定）'}\n"
                f"人数: {servings}人\n"
                f"テーマ: {theme}\nジャンル: {genre}\n"
                f"最大調理時間: {max_minutes}分\n"
                f"{want_line}\n{avoid_line}\n"
                "要件:\n"
                "- 出力は必ずSTRICTなJSONのみ（マークダウン不可）\n"
                "- 除外キーワードを含む料理名は絶対に出さない\n"
                "- 希望キーワードがあれば、少なくとも1件はその語に非常に近い料理名にする\n"
                "- 量は可能な限り具体（g, 小さじ/大さじ/個・片）で、“適量”は避ける\n"
            )
            resp = _client.chat.completions.create(
                model="gpt-4o-mini",
                temperature=FEATURES["TEMPERATURE"],
                messages=[
                    {"role": "system", "content": PROMPT_TMPL},
                    {"role": "user", "content": user_msg},
                ],
            )
            text = resp.choices[0].message.content or "{}"
            data = json.loads(text)
            parsed = RecipeSet.model_validate(data)
            return parsed
        except Exception as e:
            st.info(f"LLMの構造化生成に失敗したためフォールバックします: {e}")

    # Fallback — 最低1件
    base_ings = [Ingredient(name=x) for x in ingredients[:6]] or [Ingredient(name="鶏むね肉"), Ingredient(name="キャベツ")]
    steps = [
        Step(text="材料を食べやすい大きさに切る"),
        Step(text="フライパンで油を熱し、肉と野菜を炒める"),
        Step(text="しょうゆ・みりん・酒で味付けして全体を絡める"),
    ]
    title = (want_keyword or f"かんたん炒め（{genre}風）").strip()
    rec = Recipe(
        title=title, servings=servings, total_time_min=min(20, max_minutes),
        difficulty="かんたん", ingredients=base_ings, steps=steps, equipment=None
    )
    return RecipeSet(recommendations=[rec])

# ============================================================
# UI：入力フォーム（画像UIは非表示）
# ============================================================
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

    # 希望/除外キーワード
    st.text_input("作りたい料理名・キーワード（任意）", key="want_keyword", placeholder="例）麻婆豆腐、ナスカレー")
    st.text_input("除外したい料理名・キーワード（カンマ区切り・任意）", key="avoid_keywords", placeholder="例）麻婆豆腐, カレー")

    # 🔥 新規：栄養プロファイル選択
    st.selectbox("栄養目安プロファイル", list(NUTRI_PROFILES.keys()), index=0, key="nutri_profile")

    # 画像機能はOFFのまま（将来ONにする場合はFEATURESで制御）
    st.session_state["image_mode"] = "テキストのみ（現在のまま）"
    st.session_state["image_size"] = "1024x1024"
    st.session_state["max_ai_images"] = 0

    submitted = st.form_submit_button("提案を作成", use_container_width=True)

# 開発者向けデバッグ
if FEATURES["SHOW_DEBUG_PANEL"]:
    with st.expander("🛠 開発者向けデバッグ"):
        st.write({
            "APP_MODE": APP_MODE,
            "TEMP": FEATURES["TEMPERATURE"],
            "RETRY": FEATURES["MAX_QUALITY_RETRY"],
            "KEEP_AT_LEAST_ONE": FEATURES["KEEP_AT_LEAST_ONE"],
        })

# ------------------------------------------------------------
# 入力抽出
# ------------------------------------------------------------
if not submitted:
    st.stop()

ing_text = st.session_state.get("ingredients", "") or ""
ingredients_raw = [s for s in (t.strip() for t in re.split(r"[、,]", ing_text)) if s]
servings = int(st.session_state.get("servings", 2))
theme = st.session_state.get("theme", "節約")
genre = st.session_state.get("genre", "和風")
max_minutes = int(st.session_state.get("max_minutes", 30))
want_keyword = (st.session_state.get("want_keyword") or "").strip()
avoid_keywords = [s for s in (t.strip() for t in re.split(r"[、,]", st.session_state.get("avoid_keywords") or "")) if s]
nutri_profile = st.session_state.get("nutri_profile","ふつう")

# ============================================================
# 生成 → 品質フィルタ（✅のみ表示）＋自動リトライ
# ============================================================
try:
    data = generate_recipes(
        ingredients_raw, servings, theme, genre, max_minutes,
        want_keyword=want_keyword, avoid_keywords=avoid_keywords
    )
except Exception as e:
    st.error(f"レシピ生成に失敗しました: {e}")
    st.stop()

def _contains_any(hay: str, needles: List[str]) -> bool:
    h = (hay or "").lower()
    return any(n.lower() in h for n in needles)

# 1) タイトルで除外（安全側）
if avoid_keywords and data.recommendations:
    data.recommendations = [r for r in data.recommendations if not _contains_any(r.recipe_title, avoid_keywords)]

# 2) 希望キーワード優先
if want_keyword and data.recommendations:
    matched = [r for r in data.recommendations if want_keyword.lower() in (r.recipe_title or "").lower()]
    others  = [r for r in data.recommendations if r not in matched]
    data.recommendations = matched + others

# 3) 品質フィルタ & リトライ
if FEATURES["ENABLE_QUALITY_FILTER"]:
    attempt = 0
    passed = _filter_passed_recipes(data.recommendations)

    while not passed and attempt < FEATURES["MAX_QUALITY_RETRY"]:
        attempt += 1
        with st.spinner(f"品質に合うレシピを再提案中…（{attempt}/{FEATURES['MAX_QUALITY_RETRY']}）"):
            data = generate_recipes(
                ingredients_raw, servings, theme, genre, max_minutes,
                want_keyword=want_keyword, avoid_keywords=avoid_keywords
            )
            # 除外と希望の適用を毎回かける
            if avoid_keywords and data.recommendations:
                data.recommendations = [r for r in data.recommendations if not _contains_any(r.recipe_title, avoid_keywords)]
            if want_keyword and data.recommendations:
                matched = [r for r in data.recommendations if want_keyword.lower() in (r.recipe_title or "").lower()]
                others  = [r for r in data.recommendations if r not in matched]
                data.recommendations = matched + others

            passed = _filter_passed_recipes(data.recommendations)

    if passed:
        data.recommendations = passed
    else:
        if FEATURES["KEEP_AT_LEAST_ONE"] and data.recommendations:
            data.recommendations = [data.recommendations[0]]
            st.info("品質基準を満たす候補が見つからなかったため、参考として1件だけ表示します。")
        else:
            st.error("品質基準を満たすレシピを生成できませんでした。条件を少し緩めて再度お試しください。")
            st.stop()

# ============================================================
# 表示（✅のみバッジ表示／NGはそもそも残っていない想定）＋ 栄養概算
# ============================================================
if not data or not data.recommendations:
    st.warning("候補が作成できませんでした。入力を見直してください。")
    st.stop()

for rec in data.recommendations:
    # 表示前の正規化＆器具補完
    rec.ingredients = normalize_ingredients(rec.ingredients, rec.servings)
    tools = rec.equipment or infer_tools_from_recipe(rec)

    st.divider()
    st.subheader(rec.recipe_title)

    # 品質バッジ（OKの時だけ）
    ok, _warns = quality_check(rec)
    if ok:
        st.success("✅ 一般的な家庭料理として妥当な品質です")

    colA, colB = st.columns([2, 1])
    with colA:
        meta = []
        meta.append(f"**人数:** {rec.servings}人分")
        if rec.total_time_min:
            meta.append(f"**目安:** {rec.total_time_min}分")
        if rec.difficulty:
            meta.append(f"**難易度:** {rec.difficulty}")
        st.markdown(" / ".join(meta))

        st.markdown("**器具:** " + ("、".join(tools) if tools else "特になし"))

        # 🔥 栄養概算 & スコア表示（1人前）
        nutri = estimate_nutrition(rec)
        score = score_against_profile(nutri, nutri_profile)
        col_n1, col_n2 = st.columns([1,2])
        with col_n1:
            st.markdown("**栄養の概算（1人前）**")
            st.write(
                f"- エネルギー: {nutri['kcal']} kcal（{score['kcal']}）\n"
                f"- たんぱく質: {nutri['protein_g']} g（{score['protein_g']}）\n"
                f"- 脂質: {nutri['fat_g']} g\n"
                f"- 炭水化物: {nutri['carb_g']} g\n"
                f"- 塩分: {nutri['salt_g']} g（{score['salt_g']}）"
            )
        with col_n2:
            tips = []
            if score["salt_g"] == "⚠":
                tips.append("塩分が多め → しょうゆ/味噌を小さじ1/2減らす・だしで調整")
            if score["kcal"] == "⚠":
                tips.append("カロリー高め → 油を小さじ1→1/2、主食量を控えめに")
            if score["protein_g"] == "△":
                tips.append("たんぱく質やや不足 → 卵や豆腐を1品追加")
            if tips:
                st.info("**一言アドバイス**\n- " + "\n- ".join(tips))

        st.markdown("**材料**")
        for i in rec.ingredients:
            base, qty_in_name = split_quantity_from_name(i.name)
            amt = sanitize_amount(getattr(i, "amount", None)) or qty_in_name or "適量"
            st.markdown(
                f"- {base} {amt}"
                + ("（任意）" if i.is_optional else "")
                + (f" / 代替: {i.substitution}" if i.substitution else "")
            )

        st.markdown("**手順**")
        for idx, s in enumerate(rec.steps, 1):
            st.markdown(f"**STEP {idx}**　{strip_step_prefix(s.text)}")

    with colB:
        # 画像機能はOFF
        pass

# ここまで
