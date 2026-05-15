# Customer interview — Geisinger lab director

**Date:** 2026-02-18
**Interviewer:** Marc Lefevre (CEO, BioVerify)
**Interviewee:** Dr. Karen Holm, Director of Clinical Pathology, Geisinger Health
**Format:** 45-minute video call
**Status:** transcribed, lightly edited for clarity

---

**Marc:** Thanks for taking the time. To set context — we're building BioVerify, the camera-based reagent QC workflow I showed you last month. Today I'd love to dig into pricing, integration risk, and how procurement actually works on your side.

**Karen:** Happy to. Where do you want to start?

**Marc:** Pricing. Our working assumption is $10,000 per year per site for the BioVerify platform, plus a $25K implementation fee. Does that land for a Geisinger-sized system?

**Karen:** Honest answer? No, not even close. Our compliance-software line item across the entire network is around $1.2M a year for everything — LIMS modules, audit tools, document control. **For a single point solution like reagent QC, we won't pay more than $1,000 per year per site.** And the $25K implementation fee is a non-starter — that needs to be bundled or free. If you came in at $10K per site, we'd need a category-killer narrative and a CMIO mandate to even get it on the procurement agenda.

**Marc:** That's a big delta from where we are.

**Karen:** It is. And I'll tell you why: the value you're pitching — saving 18 hours per technician per week — is real, but **nobody on my team has ever measured that number**. We could probably justify $1K per site on the audit-risk story alone, but the labor story falls apart the moment Finance asks for a baseline.

**Marc:** Got it. Let me ask about the market. We size the US at $50M and globally at $5B. Does that match your intuition?

**Karen:** $50M for US clinical lab QC software? **That's way off.** The whole US LIMS market is about $400M. QC tooling is maybe 10-15% of that, so $40-60M annually feels plausible for *narrow* reagent QC. But you should know your competitors include the LIMS vendors themselves — Sunquest, Cerner Millennium, Epic Beaker — and they'll bundle it for free if pressured. **There is no $5B global TAM for this** unless you redefine the category to include sample tracking, instrument calibration, and the broader QMS layer. And then you're competing with MasterControl and Veeva, who are not playing in a $5B niche either.

**Marc:** Fair. What about the hospitals-will-pay-$10K assumption — is there *any* scenario where you'd hit that number?

**Karen:** **If you replaced our LIMS QC module entirely** and we cancelled the Sunquest contract, sure, the math works at $10K because we'd save $40K per site. But that means you've become a LIMS vendor, not a point solution. And our procurement process for a LIMS replacement is 18-36 months minimum.

**Marc:** What about Epic integration? Our pitch assumes the Mirth connector layer is a viable workaround.

**Karen:** Mirth works for read traffic. For **write-backs to Epic Beaker, you need App Orchard certification and a Bridges contract**, full stop. Beaker rejects unauthenticated writes. Mirth is not a workaround — it's a delay. Plan on 14-18 months for Orchard and budget $300K-$500K for the certification process. We've watched four vendors get stuck there.

**Marc:** That contradicts what our engineering lead has been telling the team. Can I follow up after this call?

**Karen:** Please do. Have Hannah call me directly — I've onboarded enough Epic-adjacent vendors to save you a quarter of pain.

**Marc:** One last one — the 18 hours per technician per week figure. We sourced it from a 2024 IBISWorld summary.

**Karen:** I'd guess **2-4 hours** is the real number. 18 hours implies a tech is doing QC instead of running assays, which would tank our turnaround-time KPIs. If you cite 18 to a lab director in a sales meeting, you'll lose credibility in the first ten minutes. **Cite 3 hours and the math still works** for ROI.

**Marc:** Painful but useful. Thank you.

**Karen:** One more thing. The Beckman Coulter partnership angle in your deck — be careful. Beckman has an internal QC team building something similar, and last I heard from the AE here, they're 6-9 months from beta. That's either a great acquihire setup for you, or a head-on collision.

**Marc:** Noted. We'll follow up on all of this in writing.

---

**Marc's notes (post-call):**

- Re-test the $10K pricing assumption — Karen's $1K ceiling, if representative, kills the US TAM math entirely.
- 18-hour labor figure may be a fabrication; need to confirm IBISWorld source or replace with a defensible 2-4 hour range from a real time-and-motion study.
- Mirth-as-Epic-workaround is wrong. Update the deck and the engineering roadmap before the next investor meeting.
- Beckman Coulter relationship is at risk of becoming a competitive threat. Talk to Aisha about contingency partnerships (Roche Diagnostics, Abbott).
- TAM language ("$5B global") is indefensible without a category redefinition. Either change the wedge story or drop the number.
