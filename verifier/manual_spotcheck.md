# Manual spot check: 3M Japan Products Limited / 3m.co.jp

The source workbook's first data row pairs this entity with `3m.co.jp`.

- [3M Japan Group company profile](https://www.3mcompany.jp/3M/ja_JP/company-jp/about-3m/group/) explicitly lists 3M Japan Products Limited among the Japanese group companies.
- [3M's 2024 subsidiaries exhibit](https://investors.3m.com/financials/sec-filings/content/0000066740-25-000006/a2024exhibit2110k.htm) lists the legal entity under 3M Company and consolidated subsidiaries.
- Attempts to open `https://3m.co.jp/` and `https://www.3m.co.jp/` through the available web retrieval tool failed. This is insufficient to establish its current redirect target or that the supplied domain is officially controlled by 3M.

**Review finding:** the entity's 3M group membership has authoritative support. The relationship of the *supplied domain* remains unverified until that domain's DNS/redirect and official-site linkage can be observed. A similarly branded domain, `3mcompany.jp`, must not be silently substituted for `3m.co.jp`.
