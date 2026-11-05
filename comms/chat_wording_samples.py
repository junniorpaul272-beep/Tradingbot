"""chat_wording_samples.py — phrasings written fresh (not by the stress generators) for each everyday-question ability. Data only; shared by stress_chat.py and test_chat.py
(kept separate so those two do not import each other: the inspector flags import cycles). First contact with the first two batches scored 74% and 67%."""
UNSEEN = {
    "price": ["whats gbpusd printing", "gimme the current quote", "where are we trading", "price check", "what's cable at", "how's gu priced right now", "tell me where price is sitting",
              "what's the rate on gbpusd at the moment", "show me where cable is", "price now?", "where's it at", "can you tell me the current gbpusd level"],
    "ema": ["are we trading over the 100", "how far is price from the ema", "price relative to moving average on the 15m", "is cable beneath the 50 ema", "we above or below the ema?",
            "has price reclaimed the 1h ema", "ema position?", "is cable holding over the 100 ema", "how close are we to the ema", "what's price doing against the moving average", "are we under the 200 ema"],
    "direction": ["bullish or bearish rn", "are we trending up or down", "what's the bias on gbpusd", "what's the market up to directionally", "is the market going up or down", "which side is in control",
                  "which way is the market leaning", "is gbpusd bullish right now", "what direction are we heading", "are buyers or sellers winning"],
    "change": ["how many pips up since this morning", "what's the move since yesterday", "has cable gained or lost since friday", "net change from 9am", "how much has it moved today",
               "performance since monday", "how many pips has cable moved since 9:30", "what's the net move since wednesday", "did gbpusd go up or down since this morning"],
    "range": ["whats the average candle size since friday", "how big are the candles today on average", "typical hourly range this week", "how volatile has it been since yesterday",
              "average range for today so far", "how large are candles on average since yesterday", "what's the usual 15m range since friday"],
    "count": ["how many times have you scanned today", "number of signals so far this week", "how many alerts did you send me", "count the scans since monday", "how many trades did i log",
              "how many scans ran today", "how many signals have fired this week", "how many setups did the bot produce since yesterday"],
    "lot": ["position size for 15 pips risk", "how many lots should i take if my stop is 11 pips", "size me for a 9 pip stop", "lots for stop 14 pips", "what lot size for a 13 pip stop",
            "how many lots can i trade with a 7 pip stop", "calculate my position size, stop is 10 pips"],
    "wellbeing": ["how's your day", "you good?", "how you been", "everything okay with you", "how are things", "are you alright", "how's it hanging"],
    "feeling_start": ["i'm not in a good place right now, need to talk", "i need some advice, feeling off", "can i talk to you about something? feeling weird", "i feel somehow today",
                      "i'm feeling a bit lost, could use some advice", "i need someone to talk to about trading", "i'm not feeling good about today"],
    "level_start": ["i've got a level in mind i'd like you to keep an eye on", "can you track a level for me", "there's a zone i want you to monitor", "i want you to watch a level for me",
                    "there's a price i'm interested in, can you track it"],
}
