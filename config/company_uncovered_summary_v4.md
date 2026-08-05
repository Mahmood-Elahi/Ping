# Uncovered company re-fingerprinting v4

This is a research and planning artifact. No source was activated and no permanent coverage ceiling is claimed.

## 1. Current reconciliation

- Original master companies: 246
- Proof: 246 = 118 + 4 + 124
- Every original company occurs exactly once; canonical and mapped sets do not overlap.

- Requested prior artifact not present in the repository: `config/company_unknown_discovery_v3.csv`. Existing v2/v3 inventories, candidates, validated registries, and validation reports were used.

## 2. Exact uncovered count

124

## 3. Covered canonical count

118

## 4. Covered mapped count

4

## 5. Production-only companies

Datadog, Elastic, GitLab, Twilio, Zscaler, CrowdStrike, HashiCorp, Confluent, Teradyne

## 6. Current platform distribution

- avature: 1
- blocked: 17
- browser_required: 7
- eightfold: 4
- greenhouse: 2
- icims: 1
- javascript_embedded_data: 12
- lever: 3
- network_inaccessible: 7
- oracle_recruiting: 2
- phenom: 1
- public_graphql: 1
- public_rest_api: 1
- sap_successfactors: 1
- stale_source: 1
- static_html: 9
- unresolved: 46
- workday: 8

## 7. Recoverability distribution

- adapter_extension_required: 17
- blocked: 20
- browser_required: 19
- network_inaccessible: 7
- ready_existing_adapter: 6
- session_required: 1
- stale_or_invalid: 1
- unresolved: 53

## 8. Ready existing-adapter candidates

Intel, NXP Semiconductors, KLA, Capital One, Unity, Loft Orbital

## 9. Ready declarative-config candidates

None

## 10. Adapter-extension candidates

onsemi, IMC Trading, Siemens, Schneider Electric, Yaskawa Electric, Aptiv, Valve, Niantic, Roblox, Zoox, Unitree Robotics, Chevron, OHB SE, Mercado Libre, Bolt, LINE, Match Group

## 11. Reusable new-adapter clusters

None

## 12. Parent-mapping-ready candidates

None

## 13. Parent-source-required candidates

None

## 14. Browser-required companies

Apple, SpaceX, Broadcom, ASML, Adyen, Jane Street, Goldman Sachs, Bosch, Aurora Innovation, Dassault Systèmes, DJI, Figure AI, Tourmaline Oil, Astrobotic, MDA Space, Intuitive Machines, Walmart Global Tech, JD.com, Patreon

## 15. Session/authentication-required companies

- Session required: Wayfair
- Authentication required: None

## 16. Blocked companies

Tesla, STMicroelectronics, Marvell Technology, Microchip Technology, Oracle, Netflix, Bloomberg, Revolut, SoFi, Nubank, Citadel Securities, Omron, Hitachi, Cognex, Mercedes-Benz, Denso, Epic Games, Costco Technology, Quora, Medium

## 17. Network-inaccessible companies

Analog Devices, BMW, CATL, Autodesk, Suncor Energy, LinkedIn, Bumble

## 18. Stale or invalid companies

BYD

## 19. Unresolved companies

Infineon Technologies, MediaTek, GlobalFoundries, Block, Tower Research Capital, Mitsubishi Electric, FANUC, Beckhoff Automation, Keyence, Porsche, Lucid Motors, Continental, Magna International, Stellantis, Sony, HTC Vive, Varjo, Canon, Sanctuary AI, SLB, Baker Hughes, Sarcos Robotics, Clearpath Robotics, OTTO Motors, Imperial Oil, Cenovus Energy, PETRONAS, ARC Resources, Precision Drilling, Firefly Aerospace, Alibaba, Temu, Instacart, Uber Eats, Flipkart, Rakuten, SHEIN, Target Tech, Best Buy, Faire, Deliveroo, Ocado Technology, ByteDance, X, Twitch, Telegram, Threads, Tencent, WeChat, Kakao, Vimeo, BeReal, Substack

## 20. Recommended next bulk rollout

Intel, NXP Semiconductors, KLA, Capital One, Unity, Loft Orbital

## 21. Estimated maximum non-browser coverage after current findings

Estimated additional non-browser recoverable findings: 23. Estimated original-list coverage: 145 of 246. Assumption: every listed ready/extension/new-adapter/parent finding passes a later implementation validation. This is not a permanent maximum or ceiling.

## 22. Estimated coverage requiring browser support

19 additional companies currently appear browser-dependent. This estimate excludes blocked, session-required, authenticated, stale, inaccessible, and unresolved sources.

## 23. Remaining strategically important custom-source targets

Infineon Technologies, MediaTek, GlobalFoundries, Block, Tower Research Capital, Mitsubishi Electric, FANUC, Beckhoff Automation, Keyence, Porsche, Lucid Motors, Continental, Magna International, Stellantis, Sony, HTC Vive, Varjo, Canon, Sanctuary AI, SLB, Baker Hughes, Sarcos Robotics, Clearpath Robotics, OTTO Motors, Imperial Oil, Cenovus Energy, PETRONAS, ARC Resources, Precision Drilling, Firefly Aerospace, Alibaba, Temu, Instacart, Uber Eats, Flipkart, Rakuten, SHEIN, Target Tech, Best Buy, Faire, Deliveroo, Ocado Technology, ByteDance, X, Twitch, Telegram, Threads, Tencent, WeChat, Kakao, Vimeo, BeReal, Substack

## 24. Classifications changed from prior research

Apple, Intel, Broadcom, NXP Semiconductors, Infineon Technologies, onsemi, KLA, Oracle, Netflix, Jane Street, Tower Research Capital, Capital One, Siemens, Schneider Electric, Bosch, FANUC, Beckhoff Automation, Keyence, Unity, Valve, Niantic, HTC Vive, Roblox, Varjo, Canon, DJI, Zoox, Unitree Robotics, Sarcos Robotics, Cenovus Energy, Astrobotic, MDA Space, Loft Orbital, OHB SE, Alibaba, Mercado Libre, Target Tech, Faire, Bolt, Ocado Technology, ByteDance, LinkedIn, Twitch, Kakao, Match Group, BeReal, Substack

All requests were anonymous and bounded. No JavaScript was executed; no browser automation, login, persistence, dashboard scan, Supabase, Google Sheets, or SQLite initialization occurred.
