# AI user-agents we inspect in robots.txt

The canonical list lives in `config/ai_user_agents.json`. We test whether each
of these is disallowed, because blocking them is an off-site discoverability
failure even when classic search crawlers are allowed.

| Token | Operator | Why it matters |
|---|---|---|
| GPTBot, OAI-SearchBot, ChatGPT-User | OpenAI | ChatGPT training, search, and on-demand fetch |
| ClaudeBot, Claude-Web | Anthropic | Claude retrieval / on-demand fetch |
| PerplexityBot | Perplexity | answer-engine retrieval |
| Google-Extended | Google | Gemini grounding / AI training |
| Applebot-Extended | Apple | Apple Intelligence training |
| Bingbot | Microsoft | Copilot grounding baseline |
| Amazonbot, meta-externalagent | Amazon, Meta | assistant retrieval |

`Googlebot` is tracked only as a classic-search baseline: if Googlebot is
allowed but AI UAs are blocked, the site is search-visible yet AI-invisible —
a precise, reportable condition.
