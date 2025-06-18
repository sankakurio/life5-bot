from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, AudioMessage, TextSendMessage,
    QuickReply, QuickReplyButton, MessageAction
)
import os, tempfile, datetime, requests, random
from dotenv import load_dotenv
from openai import OpenAI

from notion_client import Client

load_dotenv()

NOTION_MEMO_SECRET = os.getenv("NOTION_MEMO_SECRET")  # .envで管理してOK
NOTION_MEMO_PAGE_ID = os.getenv("NOTION_MEMO_PAGE_ID")  # .envで管理してOK
notion_memo = Client(auth=NOTION_MEMO_SECRET)

app = Flask(__name__)

memo_state = {}  # user_id → 進捗状態

def add_memo_to_notion(category, content, subcategory=None):
    if category == "アイデア":
        # subcategoryが未指定の場合はエラー（分岐するので必ず指定されるはず）
        if not subcategory:
            print("アイデアなのにサブカテゴリが未指定です")
            return
        block_id = CATEGORY_BLOCK_IDS["アイデア"].get(subcategory)
    else:
        block_id = CATEGORY_BLOCK_IDS.get(category)
        # もしcategoryが"アイデア"だった場合は、ここでblock_idに辞書が入ってしまう
        if isinstance(block_id, dict):
            print("カテゴリに辞書が直接渡されている。subcategory必須です。")
            return
    if not block_id:
        print(f"未知のカテゴリ: {category} {subcategory}")
        return
    text = content  # 「【カテゴリ】」は不要
    notion_memo.blocks.children.append(
        block_id=block_id,
        children=[
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {"type": "text", "text": {"content": text}}
                    ]
                }
            }
        ]
    )

print("[NOTION_REVIEW_DBID]", os.getenv("NOTION_REVIEW_DBID"))  # ←ここ！


# --- Flaskエンドポイント（404対策＋デバッグ） ---
@app.route("/", methods=["GET"])
def index():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    print("[/callback] POST accessed")
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("[/callback] InvalidSignatureError")
        abort(400)
    return "OK"

# --- LINE/OPENAI/NOTION各種セットアップ ---
line_bot_api = LineBotApi(os.getenv("LINE_CHANNEL_ACCESS_TOKEN"))
handler      = WebhookHandler(os.getenv("LINE_CHANNEL_SECRET"))
client       = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DBID  = os.getenv("NOTION_DBID")

CATEGORY_BLOCK_IDS = {
    "アイデア": {
        "仕事":      "215476859b9580af8f68c63eab51bc00",
        "プライベート": "215476859b95806d8f75c73b6b407c30",
    },
    "感情":         "215476859b958088b57bfb1a44944ebb",
    "気づき":       "215476859b9580a3bc10da74aa5dbfe9",
    "後で調べる":   "215476859b9580ff8a31ea3a6d348010",
    "タスク":       "215476859b9580dfbf65c9badda314fd",
    "買い物リスト": "215476859b95803fa180e1aab0b99d42",
    "リンク":       "215476859b95808b8cadd1eb44789038",
}

CLUSTERS = {
    "成長系":   ["誠実さ", "学び", "創造性", "自己成長", "探究心", "向上心", "努力"],
    "関係性系": ["愛", "友情", "家族", "共感", "親切", "支援", "公平"],
    "挑戦系":   ["勇気", "冒険", "達成", "主体性", "リーダーシップ", "挑戦心"],
    "安定系":   ["安定", "安心", "規律", "責任", "持続性", "調和", "安全"],
    "内面系":   ["自律", "自由", "内省", "幸福", "感謝", "精神成長"],
    "健康系":   ["健康", "体力", "活力", "バランス", "長寿", "自己管理", "ウェルネス"],
}
CLUSTER_LABELS = list(CLUSTERS.keys())
MAX_PAIRWISE   = 9

PROP_MAP = {
    "ValueStar":     "Value★",
    "ValueReason":   "Value reason",
    "MissionStar":   "Mission★",
    "MissionReason": "Mission reason",
    "IfThen":        "If-Then",
    "Tomorrow":      "TomorrowMIT",
    # ...他略
}


Q1_QUESTIONS = {
    "健康":      "🩺【健康】このままいくと後悔しそうなことは？どんな人生になりますか？",
    "挑戦経験":  "🚀【挑戦・経験】このままいくと後悔しそうなことは？どんな人生になりますか？",
    "人間関係":  "🤝【人間関係】このままいくと後悔しそうなことは？どんな人生になりますか？",
}
LIFE5_QUESTIONS = [
    None,
    None,
    "🌅【ビジョンスナップショット】未来の1シーンを30語以内で描写してください（誰と、どこで、何をして、どんな気持ち？）",
    "❓【Deep-Why】叶った時に満たされる感情 or 叶わなかった時に失うものを、感情で1行で教えてください。",
    "🎯【今日のミッション】2時間以内に、誰に対して、どんな貢献ができそうですか？"
]
progress = {}  # uid → state dict

# ---------- ユーティリティ ----------
def summarize(text: str) -> str:
    prompt = f"以下を200字以内で要約してください。\n\n{text}"
    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたは日本語の要約AIです。"},
                {"role": "user",   "content": prompt}
            ]
        )
        return res.choices[0].message.content.strip()
    except Exception as e:
        print("[要約エラー]", e)
        return text[:200]

def create_notion_row(user_id, q1_summary):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    data = {
        "parent": {"database_id": NOTION_DBID},
        "properties": {
            "Date":        {"title":[{"text":{"content":now}}]},
            "UserID":      {"rich_text":[{"text":{"content":user_id}}]},
            "Q1_Summary":  {"rich_text":[{"text":{"content":q1_summary}}]},
            "Q2_Summary":  {"rich_text":[]},
            "Q3_Summary":  {"rich_text":[]},
            "Q4_Summary":  {"rich_text":[]},
            "Q5_Summary":  {"rich_text":[]},
        }
    }
    r = requests.post("https://api.notion.com/v1/pages",
                      headers={
                          "Authorization":f"Bearer {NOTION_TOKEN}",
                          "Content-Type":"application/json",
                          "Notion-Version":"2022-06-28"
                      },
                      json=data)
    print("[Notion create]", r.status_code)
    return r.json().get("id") if r.ok else None

def update_notion_row(page_id, key, value):
    notion_key = PROP_MAP.get(key, key)          # Python で使うキー → Notion 列名
    r = requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type":  "application/json",
            "Notion-Version":"2022-06-28",
        },
        json={
            "properties": {
                notion_key: {                 # ← ここを修正
                    "rich_text": [
                        { "text": { "content": value } }
                    ]
                }
            }
        }
    )
    print("[Notion update]", notion_key, r.status_code, r.text)

def create_review_page(user_id, now):
    data = {
        "parent": {"database_id": os.getenv("NOTION_REVIEW_DBID")},
        "properties": {
            "Date":          {"title": [{"text": {"content": now}}]},
            "UserID":        {"rich_text": [{"text": {"content": user_id}}]},
            "Value★":        {"rich_text": []},
            "Value reason":  {"rich_text": []},
            "Mission★":      {"rich_text": []},
            "Mission reason":{"rich_text": []},
            "Win":           {"rich_text": []},
            "If-Then":       {"rich_text": []},
            "Pride":         {"rich_text": []},
            "Gratitude":     {"rich_text": []},
            "EmotionTag":    {"rich_text": []},
            "EmotionNote":   {"rich_text": []},
            "Insight":       {"rich_text": []},
            "TomorrowMIT":   {"rich_text": []},
        }
    }
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        json=data,
    )
    print("[Notion review page create]", r.status_code, r.text)
    return r.json().get("id") if r.ok else None

def build_pairs(values: list, n_pairs: int):
    random.shuffle(values)
    idx = list(range(len(values)))
    pairs = [(i, j) for i in idx for j in idx if i < j]
    random.shuffle(pairs)
    return pairs[:n_pairs]

def generate_ai_hint(theme, prev_inputs=None, prev_hints=None):
    # テーマ別のヒント生成プロンプト設計
    if theme == "挑戦経験":
        prompt = (
            "あなたはユーザーが人生で挑戦したいことや経験したいことに気づくための質問家です。\n"
            "以下の条件で、挑戦・経験テーマの気づきや自己対話になる問いかけやヒントを日本語で1つだけ出してください。\n"
            "【これまでの入力例】\n"
        )
        if prev_inputs:
            prompt += "\n".join([f"・{x}" for x in prev_inputs if x.strip()]) + "\n"
        if prev_hints:
            prompt += "【これまでのヒント】\n"
            prompt += "\n".join([f"・{x}" for x in prev_hints if x.strip()]) + "\n"
        prompt += (
            "【出力要件】\n"
            "- 何に挑戦したいか、どんな経験を本当はしたいのか考えるきっかけになること\n"
            "- たとえば「子供の頃の夢は？」「今やってみたいと思ってるけど先延ばしにしてることは？」「挑戦したいけど一歩踏み出せていないことは？」「やってみたかったけど諦めたことは？」など、自分の“やりたい”を思い出させる問いや気づきのヒントを1つだけ出す。\n"
            "- なるべく被らない内容、簡潔に1行で。"
        )
    else:
        prompt = (
            f"あなたは人生の後悔を防ぐための気づきや自己対話のプロ質問家です。\n"
            f"テーマ: {theme}\n"
        )
        if prev_inputs:
            prompt += "【これまでの入力例】\n"
            prompt += "\n".join([f"・{x}" for x in prev_inputs if x.strip()]) + "\n"
        if prev_hints:
            prompt += "【これまでのヒント】\n"
            prompt += "\n".join([f"・{x}" for x in prev_hints if x.strip()]) + "\n"
        prompt += (
            "これらと重複しない、ユーザーが深く考えるきっかけになるようなヒントや問いを日本語で1つだけ、1行で出してください。"
        )
    try:
        res = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "あなたは人生の問いや気づきを与えるプロの質問家です。"},
                {"role": "user",   "content": prompt}
            ]
        )
        # 1行だけ
        return [res.choices[0].message.content.strip().split("\n")[0]]
    except Exception as e:
        print("[ヒント生成エラー]", e)
        return ["（ヒント生成に失敗しました）"]

# ---------- メインロジック ----------
def life5_flow(uid, text, event, is_audio=False):
    st = progress.setdefault(uid, {})
    # 0) /life5 スタート ---------------------------------
    if text.lower() == "/life5":
        st.clear(); st["mode"]="theme"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=("人生＝時間（生まれてから死ぬまで）\n"
                      "満足した人生で終わりたい？後悔したまま？\n"
                      "死ぬ間際の後悔トップ３は ①健康 ②挑戦経験 ③人間関係\n\n"
                      "今日はどのテーマを考える？"),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label=k, text=f"テーマ:{k}"))
                    for k in Q1_QUESTIONS.keys()
                ])
            )
        )
        return True
    # 1) テーマ選択 --------------------------------------
    if text.startswith("テーマ:"):
        theme = text.replace("テーマ:", "")
        st.clear()
        st.update(theme=theme, mode="q1", q1_text="", page_id=None, hints=[])
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=Q1_QUESTIONS[theme]))
        return True

    # Q1ヒント（AI生成、重複防止付き）
    if text.strip() == "ヒント" and st.get("mode") == "q1":
        theme = st.get("theme")
        prev_inputs = [st.get("q1_text", "")]
        prev_hints  = st.get("hints", [])
        new_hint = generate_ai_hint(theme, prev_inputs, prev_hints)[0]
        # 履歴に追加
        st.setdefault("hints", []).append(new_hint)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"ヒント：\n・{new_hint}")
        )
        return True

    # 2) Q1: 後悔シナリオ入力 ----------------------------
    if st.get("mode") == "q1":
        st["q1_text"] = text
        q1_summary   = summarize(text)
        page_id      = create_notion_row(uid, q1_summary)
        st.update(page_id=page_id, mode="cluster", selected_clusters=[])
        # 要約表示
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"🔹あなたの要約：\n{q1_summary}\n\n"
                     "後悔しないために重要だと思う価値観を選んでください（2つまで）",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label=cl, text=f"クラスタ:{cl}"))
                    for cl in CLUSTER_LABELS
                ])
            )
        )
        return True

    # 3) クラスタ選択（フィルタ）--------------------------
    if text.startswith("クラスタ:") and st.get("mode") == "cluster":
        sel = text.replace("クラスタ:", "")
        if sel not in CLUSTER_LABELS or sel in st["selected_clusters"]:
            return True
        st["selected_clusters"].append(sel)
        if len(st["selected_clusters"]) < 2:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=f"もう1つ選んでください（{','.join(st['selected_clusters'])}）",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label=cl, text=f"クラスタ:{cl}"))
                        for cl in CLUSTER_LABELS if cl not in st["selected_clusters"]
                    ])
                )
            )
            return True
        values = sum([CLUSTERS[c] for c in st["selected_clusters"]], [])
        st.update(pair_vals=values, pair_scores={v:0 for v in values},
                  pairs=build_pairs(values, MAX_PAIRWISE), p_idx=0, mode="pairwise")
        i, j = st["pairs"][0]
        a, b = values[i], values[j]
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"どちらがより大事？\nA: {a}\nB: {b}",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label=f"A:{a}", text=f"ペア:{a}")),
                    QuickReplyButton(action=MessageAction(label=f"B:{b}", text=f"ペア:{b}")),
                ])
            )
        )
        return True

    # ブロック音声入力
    if st.get("mode") in ["cluster", "pairwise", "cardsort"]:
        if is_audio:
            line_bot_api.reply_message(event.reply_token,
                TextSendMessage("このステップはボタンで選んでください。"))
            return True

    # 4) ペアワイズ回答 ----------------------------------
    if text.startswith("ペア:") and st.get("mode") == "pairwise":
        val = text.replace("ペア:", "")
        if val in st["pair_scores"]:
            st["pair_scores"][val] += 1
        st["p_idx"] += 1
        if st["p_idx"] < len(st["pairs"]):
            i, j = st["pairs"][st["p_idx"]]
            a, b = st["pair_vals"][i], st["pair_vals"][j]
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=f"どちらがより大事？\nA: {a}\nB: {b}",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label=f"A:{a}", text=f"ペア:{a}")),
                        QuickReplyButton(action=MessageAction(label=f"B:{b}", text=f"ペア:{b}")),
                    ])
                )
            )
        else:
            top9 = sorted(st["pair_scores"], key=st["pair_scores"].get, reverse=True)[:9]
            st.update(cards=top9, mode="cardsort")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text="一番大事だと思う価値観を1つ選んでください",
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=MessageAction(label=card, text=f"カード:{card}"))
                        for card in top9
                    ])
                )
            )
        return True

    # 5) カードソート（1枚タップ）------------------------
    if text.startswith("カード:") and st.get("mode") == "cardsort":
        card = text.replace("カード:", "")
        if card not in st["cards"]:
            return True

        progress[uid]["latest_value"] = card
        st.update(most=card, mode="q2_reason")
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"なぜ「{card}」を選びましたか？理由を教えてください。")
        )
        return True

    # 6) 理由入力 → Notion Q2 保存 ------------------------
    if st.get("mode") == "q2_reason":
        summary = summarize(f"{st['most']}（理由：{text}）")
        if st.get("page_id"):
            update_notion_row(st["page_id"], "Q2_Summary", summary)
        st.update(mode="after", step=2)
        # 要約表示
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"🔹あなたの要約：\n{summary}\n\nあなたの最重要価値観は「{st['most']}」です！\n\n次へ進みます\n\n{LIFE5_QUESTIONS[2]}"
            )
        )
        return True

    # 7) Q3〜Q5 ------------------------------------------
    if st.get("mode") == "after":
        step = st["step"]
        if st.get("page_id"):
            summary = summarize(text)
            update_notion_row(st["page_id"], f"Q{step+1}_Summary", summary)
        else:
            summary = summarize(text)
        if step + 1 == 5:
            progress[uid]["latest_mission"] = text
        if step + 1 < len(LIFE5_QUESTIONS):
            st["step"] += 1
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=f"🔹あなたの要約：\n{summary}\n\n{LIFE5_QUESTIONS[step+1]}"
                )
            )
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(
                text=f"🔹あなたの要約：\n{summary}\n\n✅ すべて回答しました。ありがとう！"))
        return True

    return False  # 未処理

# ---------- LINE ハンドラ ----------
@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    uid  = event.source.user_id
    text = event.message.text.strip()

    # ----- ここからmemoフロー -----
    # 1. memoボタン
    if text == "memo":
        memo_state[uid] = {"step": "mode_select"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="何をしますか？",
                quick_reply=QuickReply(
                    items=[
                        QuickReplyButton(action=MessageAction(label="メモ", text="メモ")),
                        QuickReplyButton(action=MessageAction(label="呼び出し", text="呼び出し")),
                        QuickReplyButton(action=MessageAction(label="タイマー", text="タイマー")),
                    ]
                )
            )
        )
        return

    # 2. 「メモ」選択
    if memo_state.get(uid, {}).get("step") == "mode_select" and text == "メモ":
        memo_state[uid]["step"] = "category_select"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="カテゴリを選んでください",
                quick_reply=QuickReply(
                    items=[
                        QuickReplyButton(action=MessageAction(label=cat, text=cat))
                        for cat in CATEGORY_BLOCK_IDS.keys()
                    ]
                )
            )
        )
        return

    # 3. カテゴリ選択
    if memo_state.get(uid, {}).get("step") == "category_select":
        category = text
        memo_state[uid]["category"] = category

        # 「アイデア」の場合はサブカテゴリ選択
        if category == "アイデア":
            memo_state[uid]["step"] = "subcategory_select"
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text="どちらのアイデアですか？",
                    quick_reply=QuickReply(
                        items=[
                            QuickReplyButton(action=MessageAction(label="仕事", text="仕事")),
                            QuickReplyButton(action=MessageAction(label="プライベート", text="プライベート")),
                        ]
                    )
                )
            )
            return

        # それ以外のカテゴリはそのまま内容入力
        memo_state[uid]["step"] = "content_input"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("内容を入力してください")
        )
        return

    # サブカテゴリ選択
    if memo_state.get(uid, {}).get("step") == "subcategory_select":
        subcategory = text
        memo_state[uid]["subcategory"] = subcategory
        memo_state[uid]["step"] = "content_input"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("内容を入力してください")
        )
        return

    # 内容入力
    if memo_state.get(uid, {}).get("step") == "content_input":
        category = memo_state[uid].get("category")
        subcategory = memo_state[uid].get("subcategory")
        content = text
        add_memo_to_notion(category, content, subcategory)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("メモを保存しました！")
        )
        memo_state.pop(uid, None)
        return
    # ----- ここまでmemoフロー -----

    # Reviewフロー優先
    if review_flow(uid, text, event, is_audio=False):
        return
    # Life5フロー
    if not life5_flow(uid, text, event, is_audio=False):
        line_bot_api.reply_message(event.reply_token, TextSendMessage("その操作は現在のステップでは使えません。"))

@handler.add(MessageEvent, message=AudioMessage)
def handle_audio(event):
    uid = event.source.user_id
    try:
        mid = event.message.id
        audio = line_bot_api.get_message_content(mid)
        with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
            for c in audio.iter_content(): tmp.write(c); tmp.flush()
            with open(tmp.name, "rb") as f:
                txt = client.audio.transcriptions.create(model="whisper-1", file=f).text.strip()

        if memo_state.get(uid):
            # memoの進行状況によって分岐させる
            # たとえば「内容入力待ち」なら音声テキストをそのまま入れる
            if memo_state[uid].get("step") == "content_input":
                category = memo_state[uid].get("category", "未分類")
                subcategory = memo_state[uid].get("subcategory")
                content = txt
                add_memo_to_notion(category, content, subcategory)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("メモを保存しました！（音声入力）")
                )
                memo_state.pop(uid, None)
                return
            else:
                # それ以外のステップでは未対応
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage("音声は内容入力のタイミングでのみ使えます。")
                )
                return

        # --- ここで review_flowを先に通す！ ---
        if review_flow(uid, txt, event, is_audio=True):
            return
        # Life5フローも通す
        if not life5_flow(uid, txt, event, is_audio=True):
            line_bot_api.reply_message(event.reply_token, TextSendMessage("その操作は現在のステップでは使えません。"))
    except Exception as e:
        print("Whisper error:", e)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("⚠️ 音声の文字起こしに失敗しました。"))

import re

# Reviewの状態管理（ユーザーごとに進捗を記録）
review_progress = {}  # uid → state dict

REVIEW_QUESTIONS = [
    {
        "key": "ValueStar",
        "type": "star",
        "label": "Value★ の整合度を 1〜5 の★で教えてください",
        "stars": [1, 2, 3, 4, 5],
    },
    {
        "key": "ValueReason",
        "type": "star_reason",
        "label": "★を{N}にした理由を教えて！",
        "choices": ["時間が足りなかった", "集中できた", "タスクが多過ぎた", "途中で中断した", "自信があった"],
        "allow_free": True,
        "max_length": 50,
    },
    {
        "key": "MissionStar",
        "type": "star",
        "label": "Mission★ の達成度を 1〜5 の★で教えてください",
        "stars": [1, 2, 3, 4, 5],
    },
    {
        "key": "MissionReason",
        "type": "star_reason",
        "label": "★を{N}にした理由を教えて！",
        "choices": ["計画通り進めた", "思ったより難しかった", "時間が足りなかった", "集中できた", "モチベが高かった"],
        "allow_free": True,
        "max_length": 50,
    },
    {
        "key": "Win",
        "type": "text",
        "label": "今日うまくいった行動は？",
        "max_length": 100,
    },
    {
        "key": "IfThen",
        "type": "text",
        "label": "次はどう改善・自動化する？if-then 形式で 1 行で",
        "max_length": 100,
    },
    {
        "key": "Pride",
        "type": "text",
        "label": "誇りを感じた瞬間は？",
        "max_length": 100,
    },
    {
        "key": "Gratitude",
        "type": "text",
        "label": "感謝した／されたことは？",
        "max_length": 100,
    },
    {
        "key": "EmotionTag",
        "type": "emotion",
        "label": "最も強かった感情は？",
        "choices": ["喜び", "怒り", "悲しみ", "驚き", "不安"],
        "allow_free": True,
        "max_length": 100,  # ←50→100
    },
    {
        "key": "Insight",
        "type": "text",
        "label": "今日得た気づき・学びは？",
        "max_length": 100,
    },
    {
        "key": "Tomorrow",
        "type": "text",
        "label": "明日の MIT を一言で？",
        "max_length": 50,
    },
]

def review_flow(uid, text, event, is_audio=False):
    st = review_progress.setdefault(uid, {})
    print(f"[review_flow] uid={uid}, step={st.get('step')}, text='{text}'")
    # --- レビュー開始で他フローの状態を消去 ---
    if text.lower() == "/review":
        st.clear()
        st["step"] = 0
        st["answers"] = {}

        st["latest_value"] = progress.get(uid, {}).get("latest_value", "")
        st["latest_mission"] = progress.get(uid, {}).get("latest_mission", "")

        progress.pop(uid, None)
        # 新規ページをNotionに作成
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        page_id = create_review_page(uid, now)
        st["page_id"] = page_id
        ask_review_question(uid, event, 0)
        return True

    if "step" not in st:
        print("[review_flow] step not in st!", st)
        return False  # このフロー外

    step = st["step"]
    q = REVIEW_QUESTIONS[step]

    # 音声入力の許可判定（star_reasonとemotionも許可）
    if is_audio and q["type"] not in ("text", "star_reason", "emotion"):
        line_bot_api.reply_message(event.reply_token, TextSendMessage("このステップはボタンで選んでください。"))
        return True

    # star型（★選択）
    if q["type"] == "star":
        if re.fullmatch(r"[★☆]{1,5}", text) or (text.isdigit() and 1 <= int(text) <= 5):
            val = str(text.count("★")) if "★" in text else str(text)
            st["answers"][q["key"]] = val

            # ★ ValueStarのときだけNotionページ新規作成
            if q["key"] == "ValueStar" and "page_id" not in st:
                now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                page_id = create_review_page(uid, now)
                st["page_id"] = page_id

            # --- ここで即時保存 ---
            if "page_id" in st and st["page_id"]:
                update_notion_row(st["page_id"], q["key"], val)

            st["step"] += 1
            ask_review_question(uid, event, st["step"], prev_star=val)
            return True
        # QuickReply以外は弾く
        star_labels = [f"{'★'*n}{'☆'*(5-n)}" for n in range(1,6)]
        if text not in star_labels and text not in [str(n) for n in range(1,6)]:
            line_bot_api.reply_message(event.reply_token, TextSendMessage("1〜5 の★で選んでください。"))
            return True

    # star_reason型
    elif q["type"] == "star_reason":
        # 音声時は要約・100字制限
        if is_audio:
            text = summarize(text)[:100]
        if text in q["choices"]:
            st["answers"][q["key"]] = text
        elif len(text) <= 100:
            st["answers"][q["key"]] = text
        else:
            # 自動要約＆100字以内で保存
            text = summarize(text)[:100]
            st["answers"][q["key"]] = text
        if "page_id" in st and st["page_id"]:
            update_notion_row(st["page_id"], q["key"], text)
        st["step"] += 1
        ask_review_question(uid, event, st["step"])
        return True

    # emotion型（選択＋任意補足）
    elif q["type"] == "emotion":
        if "EmotionTag_main" not in st:
            if text in q["choices"]:
                st["EmotionTag_main"] = text
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="必要なら100字以内で感情の補足を入力してください（スキップ可）",
                        quick_reply=QuickReply(items=[
                            QuickReplyButton(action=MessageAction(label="スキップ", text="スキップ"))
                        ])
                    )
                )
                if "page_id" in st and st["page_id"]:
                    update_notion_row(st["page_id"], q["key"], text)
                return True
            else:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="感情タグを１つ選んでください。",
                        quick_reply=QuickReply(items=[
                            QuickReplyButton(action=MessageAction(label=tag, text=tag))
                            for tag in q["choices"]
                        ])
                    )
                )
                return True
        # 補足説明（スキップor自由入力）ここも音声・100字制限
        if text == "スキップ":
            st["answers"][q["key"]] = st.pop("EmotionTag_main")
            st["answers"]["EmotionNote"] = ""
            if "page_id" in st and st["page_id"]:
                update_notion_row(st["page_id"], q["key"], st["answers"][q["key"]])
                update_notion_row(st["page_id"], "EmotionNote", "")
            st["step"] += 1
            ask_review_question(uid, event, st["step"])
            return True
        # ここで音声入力や長文も要約100字
        note = summarize(text)[:100] if (is_audio or len(text) > 100) else text
        st["answers"][q["key"]] = st.pop("EmotionTag_main")
        st["answers"]["EmotionNote"] = note
        if "page_id" in st and st["page_id"]:
            update_notion_row(st["page_id"], q["key"], st["answers"][q["key"]])
            update_notion_row(st["page_id"], "EmotionNote", note)
        st["step"] += 1
        ask_review_question(uid, event, st["step"])
        return True

    # text型
    elif q["type"] == "text":
        # 音声または100字超→自動要約
        if is_audio or len(text) > q["max_length"]:
            text = summarize(text)[:q["max_length"]]
        st["answers"][q["key"]] = text
        if "page_id" in st and st["page_id"]:
            update_notion_row(st["page_id"], q["key"], text)
        st["step"] += 1
        if st["step"] >= len(REVIEW_QUESTIONS):
            review_progress.pop(uid, None)
            line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ Reviewの入力が完了しました。ありがとう！"))
            return True
        ask_review_question(uid, event, st["step"])
        return True

    # 終了判定
    if st["step"] >= len(REVIEW_QUESTIONS):
        save_review_to_notion(uid, st["answers"])
        review_progress.pop(uid, None)
        line_bot_api.reply_message(event.reply_token, TextSendMessage("✅ Reviewの入力が完了しました。ありがとう！"))
        return True

    return False

def ask_review_question(uid, event, step, prev_star=None):
    if step >= len(REVIEW_QUESTIONS):
        return
    q = REVIEW_QUESTIONS[step]

    user_st = review_progress.get(uid, {})
    latest_value = user_st.get("latest_value", "")
    latest_mission = user_st.get("latest_mission", "")

    # ValueStar: 価値観
    if q["key"] == "ValueStar":
        label = (
            f"今日の価値観は「{latest_value}」でした。Value★ の整合度を 1〜5 の★で教えてください"
            if latest_value else "Value★ の整合度を 1〜5 の★で教えてください"
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=label,
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(
                        label=f"{'★'*n}{'☆'*(5-n)}", text=str(n))) for n in q["stars"]
                ])
            )
        )
        return

    # MissionStar: ミッション
    if q["key"] == "MissionStar":
        label = (
            f"今日のミッションは「{latest_mission}」でした。Mission★ の達成度を 1〜5 の★で教えてください"
            if latest_mission else "Mission★ の達成度を 1〜5 の★で教えてください"
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=label,
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(
                        label=f"{'★'*n}{'☆'*(5-n)}", text=str(n))) for n in q["stars"]
                ])
            )
        )
        return
    # （以下略：star_reason, emotion, text型は現状のままでOK）

    # ★理由
    elif q["type"] == "star_reason":
        prev_n = prev_star if prev_star is not None else ""
        label = q["label"].replace("{N}", prev_n)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=label,
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label=choice, text=choice))
                    for choice in q["choices"]
                ])
            )
        )
        return
    # emotion
    elif q["type"] == "emotion":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=q["label"],
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=MessageAction(label=tag, text=tag)) for tag in q["choices"]
                ])
            )
        )
        return
    # テキスト
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=q["label"]))

def save_review_to_notion(uid, answers):
    print("[save_review_to_notion] DBID:", os.getenv("NOTION_REVIEW_DBID", "Review_Log"))
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    data = {
        "parent": {"database_id": os.getenv("NOTION_REVIEW_DBID", "Review_Log")},
        "properties": {
            "Date":          {"title": [{"text": {"content": now}}]},
            "UserID":        {"rich_text": [{"text": {"content": uid}}]},
            "Value★":        {"rich_text": [{"text": {"content": answers.get("ValueStar", "")}}]},
            "Value reason":  {"rich_text": [{"text": {"content": answers.get("ValueReason", "")}}]},
            "Mission★":      {"rich_text": [{"text": {"content": answers.get("MissionStar", "")}}]},
            "Mission reason":{"rich_text": [{"text": {"content": answers.get("MissionReason", "")}}]},
            "Win":           {"rich_text": [{"text": {"content": answers.get("Win", "")}}]},
            "If-Then":       {"rich_text": [{"text": {"content": answers.get("IfThen", "")}}]},
            "Pride":         {"rich_text": [{"text": {"content": answers.get("Pride", "")}}]},
            "Gratitude":     {"rich_text": [{"text": {"content": answers.get("Gratitude", "")}}]},
            "EmotionTag":    {"rich_text": [{"text": {"content": answers.get("EmotionTag", "")}}]},
            "EmotionNote":   {"rich_text": [{"text": {"content": answers.get("EmotionNote", "")}}]},
            "Insight":       {"rich_text": [{"text": {"content": answers.get("Insight", "")}}]},
            "TomorrowMIT":   {"rich_text": [{"text": {"content": answers.get("Tomorrow", "")}}]},
        }
    }
    r = requests.post(
        "https://api.notion.com/v1/pages",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        json=data,
    )
    print("[Notion review create]", r.status_code, r.text)

if __name__ == "__main__":
    app.run(port=5000)
