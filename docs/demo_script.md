# ExportScout: demo video script (under 3:00)

Based on plan §13. Record in **Demo Mode** so the run is repeatable and spends no credits. Say out loud that it's a replay of recorded live searches. Every number you read comes from the screen: **don't script or invent numbers**. Read them off the brief during the take.

## Before you record

- `streamlit run app.py`, browser at 100% zoom, window about 1400 px wide, light theme.
- Sidebar:
  - Demo Mode **on**
  - Credit budget **45**
  - **Compare other markets** on (free in Demo Mode)
  - Assumptions expander closed
- Demo product: **Brass hurricane lantern** (unit cost ₹650, extra costs ₹60, MOQ 200 are prefilled).
- Do one full dry run first, so you know where each number sits on screen.
- In the dry run, check that the Buyers tab shows at least one company in **UK companies hiring buyers now**. If it shows none, drop the hiring line.
- Only use a real exporter's quote if you have their written permission. Otherwise leave it out.
- Have the architecture diagram ready (the README's mermaid chart, rendered on GitHub).

## Script

| Time | On screen | What to click | Voice-over (approx.) |
|---|---|---|---|
| 0:00–0:18 | App header; open the **Why now** expander | Click **Why now** | "Moradabad ships about 75% of its brassware to the US. In August 2025 the US tariff hit 50%, and orders worth over ₹300 crore stopped. Since 15 July 2026, the UK lets these products in duty-free. But a 30-worker workshop doesn't know who to sell to there, or at what price." |
| 0:18–0:30 | **Your product** section | Close **Why now**. Show the demo product select, the cost ₹650 and MOQ 200. Point at the disabled **Market: United Kingdom** select | "The owner gives ExportScout one photo, a unit cost and an MOQ. The target market is the UK." |
| 0:30–1:08 | The **Scouting the UK market…** status box with the live step log; sidebar credit meter | Click **Scout the UK market**. Let the log run; don't cut | "The agent searches the UK market through SerpApi. Lens finds UK look-alikes. Trends shows when demand peaks. Shopping, Amazon and eBay build the price ladder. Amazon product pages show where competing products are made, and reviews show complaints. Then it finds UK buyers, including companies hiring a buyer right now, and prices the same product in four more countries. It picks its next searches from what it finds, and the meter shows the credit budget." |
| 1:08–1:30 | **Brief** tab | Point at the verdict badge, the quote range (£ and ₹) and the margin. Point at **Best other market**. Scroll to the price chart and the complaints | "Here's the answer: a FOB quote range, the margin at today's exchange rate, and a Go verdict. It also shows the top complaints UK customers have, and the best other market to try." |
| 1:30–1:45 | **Markets** tab | Open **Markets**. Point at the ranked table, then the margin-by-market chart and the US duty | "The same product in four more markets. The UK, the UAE and Australia are zero duty; the US now pays [read the US duty off the screen]. They're ranked by Market Fit." |
| 1:45–2:10 | **Buyers** tab | Open **Buyers** and show the ranked table; the top buyer's expander is open. Point at the **Hiring** column and the **UK companies hiring buyers now** card | "Here are the buyers, ranked. For each one you can see why: ads running in the UK, a trade-account page, made-in-India products. Every point links to the search result behind it. And these companies are hiring a buyer now, so they're building next season's range." |
| 2:10–2:25 | **Pitch** tab | Open **Pitch**. Scroll the email. Point at **Spec improvements** and **Compliance notes** | "A pitch email for that buyer, built on their own signals and on the fixes UK customers ask for. ExportScout never sends anything: the exporter decides." |
| 2:25–2:42 | **Evidence** tab, then the architecture diagram | Open **Evidence**. Point at the "N SerpApi engines used" chart title. Cut to the mermaid diagram | "Every number traces back to a search result, with its engine and a timestamp. It uses 13 SerpApi engine APIs, and a live run is capped at 45 credits. Demo Mode replays recorded searches, so anyone can try it without keys." |
| 2:42–2:57 | Back to the Brief headline | none | "Next: full deep dives for more markets, and other clusters like Jaipur pottery. It's the same pipeline with a new config file. ExportScout: one product photo, and UK buyers come back." |

## After recording

- Check the video is under 3:00 and plays in an incognito window.
- Check that no API key, `.env` file or terminal with keys appears on screen.
