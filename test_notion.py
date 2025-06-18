from notion_client import Client

notion = Client(auth="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")

def add_memo_to_notion(category, content):
    page_id = "215476859b9580ceb4c7d76731f1c281"
    text = f"【{category}】{content}"
    notion.blocks.children.append(
        block_id=page_id,
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

memo_state = {}  # user_id → 進捗記録

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_message = event.message.text.strip()
    user_id = event.source.user_id

    # 1. memo
    if user_message == "memo":
        memo_state[user_id] = {"step": "mode_select"}
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
    if memo_state.get(user_id, {}).get("step") == "mode_select" and user_message == "メモ":
        memo_state[user_id]["step"] = "category_select"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="カテゴリを選んでください",
                quick_reply=QuickReply(
                    items=[
                        QuickReplyButton(action=MessageAction(label="アイデア", text="アイデア")),
                        QuickReplyButton(action=MessageAction(label="感情", text="感情")),
                        # 他カテゴリ
                    ]
                )
            )
        )
        return

    # 3. カテゴリ選択
    if memo_state.get(user_id, {}).get("step") == "category_select":
        memo_state[user_id]["category"] = user_message
        memo_state[user_id]["step"] = "content_input"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("内容を入力してください")
        )
        return

    # 4. 内容入力
    if memo_state.get(user_id, {}).get("step") == "content_input":
        category = memo_state[user_id].get("category", "未分類")
        content = user_message
        add_memo_to_notion(category, content)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage("メモを保存しました！")
        )
        memo_state.pop(user_id, None)  # 終了したら状態リセット
        return

    # その他の場合
    line_bot_api.reply_message(event.reply_token, TextSendMessage("もう一度最初からやり直してください。"))
