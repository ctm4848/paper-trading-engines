import re
SPORTSLUG=re.compile(r"^(nba|nfl|nhl|mlb|epl|ucl|uel|mls|ncaa|cfb|cbb|wnba|ufc|atp|wta|f1|lol|cs2|csgo|dota2?|val|bun|lal|sea|fl1|ere|por|tur|arg|bra|mex|jpn|kor|chn|aus|rou1|den|nor|swe|cri|ipl|bkn|cwbb|sasl|itsb|elc|spl|uef|kbo|npb|euro|copa|fifwc|wc|ow|r6|rl|codmw|mlbb|pga|lpga|nascar|box|mma|cricket|t20|odi|test|afl|nrl|rugby|six|wcq|unl|ucol|cdr|bl2|sb|lig|kxl)\d?-",re.I)
GAMESLUG=re.compile(r"^[a-z0-9]{2,8}-[a-z0-9]{2,8}-[a-z0-9]{2,8}-20\d\d-\d\d-\d\d",re.I)
SPORTKW=re.compile(r"\b(vs\.?|o/u|spread|win on 20|tennis|golf|boxing|grand prix|masters tournament|open\b|super bowl|world series|stanley cup|nba|nfl|nhl|mlb|premier league|champions league|world cup|olympic|medal|esports|counter-strike|dota|valorant|league of legends|fc\b|map \d|game \d|exact score|halftime|ballon d|heisman|mvp|draft|playoffs|finals|wimbledon|us open|french open|australian open|tour de france|f1|formula 1|ufc|fight|bout|kickoff)",re.I)
CRYPTO_SH=re.compile(r"(up or down|updown|above .* on (january|february|march|april|may|june|july|august|september|october|november|december)|price of (bitcoin|ethereum|solana|xrp).*(on|at)|bitcoin above|ethereum above|solana above|xrp above|between \$)",re.I)
CRYPTO=re.compile(r"\b(bitcoin|btc|ethereum|eth|solana|sol|xrp|doge|dogecoin|crypto|bnb|hyperliquid|hype|memecoin|fdv|airdrop|token|coinbase|microstrategy|stablecoin|tether|usdt|usdc|binance|altcoin|megaeth|pump\.fun)\b",re.I)
MACRO=re.compile(r"\b(fed|fomc|interest rates?|rate cut|rate hike|bps|cpi|inflation|gdp|recession|unemployment|jobs report|payrolls|nfp|pce|treasury|yields?|powell|ecb|boe|boj)\b",re.I)
FIN=re.compile(r"\b(s&p|spx|nasdaq|dow|stock|shares|earnings|ipo|market cap|tesla|nvidia|apple|microsoft|amazon|google|meta|oil|crude|gold|silver|copper|eur/usd|usd|close (above|below)|largest company)\b",re.I)
TECH=re.compile(r"\b(ai|openai|gpt|chatgpt|gemini|claude|anthropic|grok|xai|llm|model|spacex|starship|iphone|apple event|launch|neuralink|tiktok|app store)\b",re.I)
POL=re.compile(r"\b(election|elections|elected|trump|biden|harris|vance|president|presidential|senate|house|governor|mayor|congress|republican|democrat|gop|parliament|prime minister|vote|primary|nominee|cabinet|minister|chancellor|seats|impeach|supreme court|scotus|executive order|approval rating|poll|mamdani|newsom|pardon|resign|deport|shutdown|tariff)\b",re.I)
GEO=re.compile(r"\b(ukraine|russia|putin|zelensky|israel|gaza|hamas|hezbollah|iran|china|xi|taiwan|north korea|kim jong|nato|ceasefire|war|strike|invade|invasion|military|missile|nuclear|peace deal|hostage|venezuela|maduro|syria|houthi|pope|strait of hormuz)\b",re.I)
CULT=re.compile(r"\b(box office|movie|film|album|song|spotify|billboard|grammy|oscar|emmy|taylor swift|kanye|celebrity|youtube|mrbeast|tweets?|post|say|mention|netflix|tv|show|eurovision|time person|weather|temperature|hurricane|snow|rain|°f|°c)\b",re.I)
def classify(title,slug=""):
    t=title or ""; s=slug or ""
    if SPORTSLUG.match(s) or GAMESLUG.match(s): return "sports"
    if CRYPTO.search(t) or "updown" in s:
        return "crypto_short" if (CRYPTO_SH.search(t) or "updown" in s) else "crypto_other"
    if SPORTKW.search(t): return "sports"
    if MACRO.search(t): return "macro"
    if GEO.search(t): return "geopolitics"
    if POL.search(t): return "politics"
    if FIN.search(t): return "finance"
    if TECH.search(t): return "tech_ai"
    if CULT.search(t): return "culture/weather/mentions"
    return "other"
