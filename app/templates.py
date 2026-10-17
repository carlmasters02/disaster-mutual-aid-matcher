"""Deterministic multilingual templates — the fallback floor under every LLM call."""

NEED_JA = {"wheelchair": "車椅子", "walker": "歩行器", "bedridden": "寝たきり", "dementia": "認知症",
           "visual": "視覚障害", "hearing": "聴覚障害", "oxygen": "在宅酸素", "dialysis": "人工透析",
           "infant": "乳幼児", "non_japanese_speaker": "日本語以外", "pet": "ペット同行"}
NEED_EN = {"wheelchair": "wheelchair", "walker": "walker", "bedridden": "bedridden", "dementia": "dementia",
           "visual": "low vision", "hearing": "hard of hearing", "oxygen": "home oxygen",
           "dialysis": "dialysis", "infant": "infant", "non_japanese_speaker": "non-Japanese speaker",
           "pet": "pet"}
LANG_JA = {"ja": "日本語", "en": "英語", "zh": "中国語", "vi": "ベトナム語", "ko": "韓国語",
           "ne": "ネパール語", "pt": "ポルトガル語"}
LANG_EN = {"ja": "Japanese", "en": "English", "zh": "Chinese", "vi": "Vietnamese", "ko": "Korean",
           "ne": "Nepali", "pt": "Portuguese"}

ITEMS = {  # need -> (ja, en)
    "oxygen": ("予備酸素ボンベ・バッテリー", "spare oxygen & battery"),
    "dialysis": ("透析手帳", "dialysis notebook"),
    "dementia": ("名札・連絡先カード", "name tag & contact card"),
    "infant": ("ミルク・おむつ", "formula & diapers"),
    "pet": ("ケージ・リード", "pet carrier & leash"),
    "hearing": ("筆談ボード", "writing board"),
    "visual": ("白杖", "white cane"),
    "bedridden": ("担架用毛布", "blanket for stretcher"),
}
BASE_ITEMS = (["常備薬", "お薬手帳", "水", "懐中電灯"], ["medicine", "medication notebook", "water", "flashlight"])

HANDLING = {  # need -> (ja, en)
    "wheelchair": ("車椅子のまま段差を2人で持ち上げる", "lift the wheelchair over steps with two people"),
    "bedridden": ("毛布担架で3人搬送", "carry on a blanket stretcher, 3 people"),
    "walker": ("階段は手すり側で介助", "assist on stairs on the handrail side"),
    "oxygen": ("酸素チューブを踏まない・火気厳禁", "keep oxygen tube clear, no open flame"),
    "dementia": ("ゆっくり名前を呼んで安心させる", "speak slowly, use their name to reassure"),
    "hearing": ("筆談・身ぶりで伝える", "use writing and gestures"),
    "visual": ("肩を貸して声で誘導", "offer your shoulder and guide by voice"),
    "infant": ("抱っこ紐を使う", "use a baby carrier"),
}


def items_for(needs: list[str]) -> tuple[list[str], list[str]]:
    ja, en = list(BASE_ITEMS[0]), list(BASE_ITEMS[1])
    for n in needs:
        if n in ITEMS:
            ja.insert(0, ITEMS[n][0])
            en.insert(0, ITEMS[n][1])
    return ja, en


def handling_for(needs: list[str], mobility: str) -> tuple[str, str]:
    keys = [mobility] + [n for n in needs if n != mobility]
    ja = [HANDLING[k][0] for k in keys if k in HANDLING]
    en = [HANDLING[k][1] for k in keys if k in HANDLING]
    return ("、".join(ja) or "特記なし", "; ".join(en) or "none")


DISPATCH = {
    "ja": "【出動要請】{block}の{pid}さん（{needs}）の避難支援をお願いします。到着目安{eta}分。避難先: {shelter}。持ち物: {items}。注意: {notes}。道が塞がれていたら無理せず迂回し、本部に連絡してください。",
    "en": "[Dispatch] Please help {pid} in {block} ({needs}). ETA {eta} min. Take them to: {shelter}. Bring: {items}. Note: {notes}. If the road is blocked, don't force it: detour and report to HQ.",
    "zh": "【出动请求】请协助{block}的{pid}（{needs}）避难。预计{eta}分钟到达。避难所：{shelter}。携带：{items}。注意：{notes}。如道路被堵，请勿强行通过，绕行并联系指挥部。",
    "vi": "[Yêu cầu hỗ trợ] Vui lòng giúp {pid} ở {block} ({needs}) sơ tán. Dự kiến {eta} phút. Nơi sơ tán: {shelter}. Mang theo: {items}. Lưu ý: {notes}. Nếu đường bị chặn, đừng cố đi qua — hãy đi vòng và báo cho trung tâm.",
    "ko": "[출동 요청] {block}의 {pid}님({needs}) 대피를 도와주세요. 도착 예상 {eta}분. 대피소: {shelter}. 준비물: {items}. 주의: {notes}. 길이 막혀 있으면 무리하지 말고 우회한 뒤 본부에 연락하세요.",
    "pt": "[Chamado] Por favor, ajude {pid} em {block} ({needs}) a evacuar. Chegada em {eta} min. Abrigo: {shelter}. Leve: {items}. Atenção: {notes}. Se a rua estiver bloqueada, não force — desvie e avise a central.",
    "ne": "[सहायता अनुरोध] कृपया {block} मा {pid} ({needs}) लाई सुरक्षित स्थानमा लैजान मद्दत गर्नुहोस्। अनुमानित {eta} मिनेट। आश्रयस्थल: {shelter}। साथमा: {items}। ध्यान: {notes}। बाटो बन्द भए जबरजस्ती नगर्नुहोस् — अर्को बाटो लिनुहोस् र मुख्यालयलाई खबर गर्नुहोस्।",
}

RESIDENT = {
    "ja": "{name}さんへ\n地震のときは、{helpers}が あなたの 家に 来ます（{eta}分くらい）。\nいっしょに「{shelter}」へ 行きます。\n持ち物: {items}\nあわてずに、家で まってください。",
    "en": "Dear {name},\nIf there is an earthquake, {helpers} will come to your home (about {eta} min).\nYou will go together to \"{shelter}\".\nPlease prepare: {items}\nStay calm and wait at home.",
    "zh": "{name}您好：\n地震时，{helpers}会到您家（约{eta}分钟）。\n大家一起去“{shelter}”。\n请准备：{items}\n请不要慌张，在家等候。",
    "vi": "Kính gửi {name},\nKhi có động đất, {helpers} sẽ đến nhà bạn (khoảng {eta} phút).\nMọi người sẽ cùng đi đến \"{shelter}\".\nHãy chuẩn bị: {items}\nĐừng hoảng sợ, hãy đợi ở nhà.",
    "ko": "{name}님께\n지진이 나면 {helpers}이(가) 댁으로 갑니다(약 {eta}분).\n함께 \"{shelter}\"(으)로 갑니다.\n준비물: {items}\n당황하지 마시고 집에서 기다려 주세요.",
    "pt": "Prezado(a) {name},\nEm caso de terremoto, {helpers} irá até sua casa (cerca de {eta} min).\nVocês irão juntos para \"{shelter}\".\nPrepare: {items}\nMantenha a calma e espere em casa.",
    "ne": "{name} ज्यू,\nभूकम्प आएमा {helpers} तपाईंको घरमा आउनुहुनेछ (करिब {eta} मिनेट)।\nसँगै \"{shelter}\" जानुहुनेछ।\nतयार राख्नुहोस्: {items}\nनआत्तिनुहोस्, घरमै पर्खनुहोस्।",
}

ESCALATION_JA = {
    "unreachable": "すべての避難経路が道路閉塞・火災で寸断されています。消防・警察による救助が必要です。",
    "no_candidates": "この時間帯に条件（介助技能・稼働）を満たす協力者が地域にいません。",
    "eta_exceeded": "15分以内に到着できる協力者がいません（経路寸断・遠距離）。",
    "capacity_exhausted": "近隣の協力者は全員、より優先度の高い要支援者に割当済みです。",
}
RESOURCES = {
    "bedridden": "担架と救急隊員3名", "wheelchair": "車椅子対応車両", "oxygen": "酸素ボンベの搬送・電源確保",
    "dialysis": "透析可能な医療機関への搬送調整", "dementia": "顔見知りの同行者", "infant": "乳児用物資",
    "non_japanese_speaker": "通訳（多言語支援センター）",
}
