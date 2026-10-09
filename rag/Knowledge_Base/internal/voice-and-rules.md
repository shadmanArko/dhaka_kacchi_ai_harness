---
title: Dhaka Kacchi Berlin — Voice, Audiences and Rules
collection: rules            # knowledge-base collection; INTERNAL (content agents and checker, never the customer bot)
status: DRAFT v0.1 — 2026-10-06; [CONFIRM] = owner decision needed
related: docs/brand/brand-book.md, docs/brand/facts.yaml, config/content/playbook.yaml
---

# Voice, audiences and rules

## 1. Brand personality

We sound like **a warm host from Dhaka who loves feeding people**: proud of the food, honest about being small, a
little playful, never pushy.

| We are | We are not |
|---|---|
| warm, generous, personal ("we", "our kitchen", real stories) | corporate, anonymous |
| proud of tradition (Old Dhaka, dum, wedding kacchi) | arrogant about other cuisines |
| sensory (steam, aroma, fluffy rice, juicy mutton) | generic ("delicious food") |
| honest (small batches, Saturdays only, real prices) | fake scarcity, fake reviews |
| playful (Team Aloo vs Team Meat, "lambs at the spice spa") | mocking, edgy, political |
| inclusive (everyone at our table) | only for one community |

**Lines that capture the voice** (from our own posts, reuse freely):
- "One bowl. Endless layers of flavor." (our best-performing post: 16,383 reach)
- "Not everyone can fly to Dhaka today… but you can taste a piece of Old Dhaka in Berlin."
- "Sometimes it's not about selling food. It's about sharing a little piece of home."
- "The moment you open a Dhaka Kacchi handi…"
- "Kacchi rice must be fluffy, separate, and full of flavour."
- "Breaking news: the lambs have checked into our spice spa…"
- "Proof that our Borhani deserves its own order."

## 2. Words

**Always:**
- **Kacchi** (with the brand: "Dhaka Kacchi"); **Berlin** in every caption.
- *dum*, *handi* (the pot), *beresta*, *aloo*, *Borhani*, *Old Dhaka* / *Dhakaiya*, *slow-cooked*, *fresh every
  Saturday*.

**Spelling** [CONFIRM one standard]:
- Our posts mix "Biryani", "Biriyani" and "Biryiani". Use **Kacchi Biryani** in German and English captions: it's
  the spelling people search for.
- Use "Kacchi Biriyani" in Bengali-community posts if preferred.
- The brand name is always **Dhaka Kacchi**, written that way, never "Dhaka kacchi".

**Avoid:**
- "best in Berlin", "Nr. 1", "Berlin's only real kacchi", "standards other kitchens can't match". These are §5/§6
  UWG risks (section 7).
- Health promises.
- "Fusion".
- Generic adjectives without detail ("tasty", "yummy" alone).

## 3. Languages

| Audience | Language | How |
|---|---|---|
| Everyone in Berlin (the default) | **German first**, then one English line | Every public post. None of our 417 posts so far is in German: the biggest untested opportunity. |
| International Berlin | English | Second line, or full English on Threads and X |
| Bangladeshi community | Bengali or Banglish (Bengali in Latin letters) | Community posts, Facebook groups, Eid, emotional stories. Our most emotional posts were Bengali. |
| Indian, Pakistani, Arab, Turkish and other communities | Greeting in their script (from config/taxonomies/communities.yaml), plus German or English | Occasion posts and community-bridge posts |

German tone: use *du* on Instagram, TikTok and Threads; *Sie* or neutral on Facebook and LinkedIn [CONFIRM].
Native-speaker check is required for any non-German or non-English line marked `needs_native_review`.

## 4. Religion and sensitive topics

- **Never use religious ritual or deity words of other faiths** (the `blocked_words` list in playbook.yaml).
  Festivals are a "festive season"; we celebrate the mood and the food.
- **Islamic phrases** (Alhamdulillah, In sha Allah, MashaAllah, Eid Mubarak) are part of the founder's personal
  voice and are welcomed by the Bangladeshi and Muslim community. [CONFIRM the policy] Suggested:
  - Keep them in Bengali-community posts.
  - Use "Eid Mubarak" for Eid posts.
  - Leave them out of German or English sales posts aimed at all of Berlin, so no audience feels excluded.
- **No politics**, no national rivalries beyond friendly food fun (e.g. "Kolkata vs Dhaka biryani" is fine if it
  stays warm).
- **Solemn days** (Ashura, 21 February, remembrance days) get a respectful message only, never a sales post.

## 5. Audiences

| Audience | Why they buy | What to show | Evidence |
|---|---|---|---|
| **Bangladeshi in Berlin and Germany** (3,600 citizens; more with German passports) | nostalgia, "taste of home", Eid, weddings | emotional stories, Bengali captions, Eid specials | our most emotional posts; customers from Hamburg and Munich |
| **Indian and Pakistani** (49,500 and 9,700) | biryani lovers; a "different biryani" curiosity | the kacchi vs biryani explainer, dum reveal, festive-season posts | served "Bangladeshi, Pakistani & Indian food lovers" (18 July) |
| **Arab, Turkish, Afghan, Persian** (Turkish 107,000, Syrian 40,700, Afghan 24,800) | meat-and-rice culture (mansaf, pilav, palaw) | the community-bridge posts, halal, family platters | own posts tagging #TurkishFoodLoversBerlin #ArabFoodBerlin |
| **Germans and international Berliners** | discovery, weekend treat, value | German captions, "first time eating kacchi", Borhani discovery, price per box | the German customer who came back for Borhani (2,900 reach) |
| **Offices and event organisers** | catering | group orders, reliability, hygiene | catering post (23 June) |

## 6. What works on our own account (data, Apr–Sep 2026)

- **Formats:**
  - Facebook: Reels and video, plus short image posts.
  - Instagram: **Reels only**. A typical Reel reaches 118 people; images 14, carousels 16.
  - Threads: about 1 reach per post. Use it for text questions or pause it.
- **Short captions win:** under 150 characters gets 2–7× the reach of long captions. Long stories belong in a
  Facebook post or the website.
- **Hashtags:** use 1–3. None, or 4 or more, does worse.
- **Topics that worked:**
  - news (new menu, mutton is coming, taster box);
  - real moments (sold out, customer stories, "no perfect photo");
  - sensory close-ups (lid opening, fluffy rice, marination, steam);
  - a question for the audience ("Doorstep delivery: yes or no?").
- **Timing:** late afternoon, 16:00–19:00, did best on Facebook.
- **Weekly rhythm that already works:**
  - Monday or Tuesday: "slots open".
  - Midweek: process and sensory posts.
  - Thursday or Friday: deadline reminder.
  - Saturday: cooking and delivery moments.
  - Sunday: "thank you" plus a customer story.

## 7. Legal rules (Germany) and risks found in past posts

| Rule | Why | Seen in our posts | Fix |
|---|---|---|---|
| No unprovable superlatives | §5 UWG | "Best Mutton kacchi biryani in Berlin" (20 Sep); "Berlin's Only Real Kacchi" (website) | Say "Old Dhaka-style kacchi, cooked fresh every Saturday" |
| No disparaging comparisons | §6 UWG | "standards most kitchens in Berlin can't match" (website) | Describe our own standards only |
| No health claims | EU Reg. 1924/2006 | Borhani "aids digestion", "rich in probiotics" (10 June) | Say "refreshing", "traditionally served with kacchi" |
| "Halal" must be true and backed | §5 UWG | "100% Halal" in many posts | Name the halal butcher or certifier, or say "halal mutton from [supplier]" [CONFIRM] |
| Prices are final prices incl. VAT | PAngV | €15, €12.99, €9.99, €6 | Confirm VAT status; show final prices |
| Discounts need the earlier price and the 30-day lowest price | §11 PAngV | "14% off, €12.99 instead of €15" (Eid) | OK if €15 was the price in the previous 30 days; keep a record |
| Giveaways need clear terms | UWG / platform rules | Eid video contest (free box) | Publish who can join, deadline, how the winner is chosen, no purchase needed |
| "Werbung / Anzeige" for paid, gifted or creator posts | Medienanstalten guide | – | Label every collaboration |
| Consent for people on camera | §22 KUG, GDPR | children and customers in photos | Written consent, especially for children |
| Impressum on profiles | §5 DDG | – | Bio link labelled "Impressum" |

## 8. Visual style

**Hero shots:**
- the handi lid opening and the steam;
- layers cut with a spoon;
- fluffy separate rice grains;
- the dum potato;
- Borhani poured cold;
- the chutney.

**Process shots:**
- marination;
- spice roasting and blending;
- ghee on basmati;
- sealing the pot;
- 5 a.m. dum.

**People:** hands and the founder's voice; customers only with consent.

**Light and look:** warm light (about 2700 K); saffron and rice colours kept true; real kitchen, not studio.

**Format:** shoot vertical 9:16 for video and 3:4 for photos. Always our own footage, never watermarked reposts.

**Recurring emojis:** 🍛 🔥 ❤️‍🔥 🩷 📍Berlin 🥛. Use at most 3–4 per caption.

## 9. Open decisions for the owner

1. Main tagline (brand book, section 1).
2. Spelling: "Biryani" or "Biriyani".
3. The current menu and prices: mutton price, whether lamb and the taster box remain, and the chutney price
   (facts.yaml).
4. One order deadline (Friday 18:00?) and the minimum order.
5. Pickup point wording: "Leopoldplatz" or "our Wedding kitchen".
6. Halal source or certifier; "never frozen" claim; food business registration status.
7. Allergen list.
8. Islamic-phrase policy for broad posts (section 4).
9. *du* or *Sie* in German.
10. Website wording to change: "Berlin's Only Real Kacchi" and "standards most kitchens can't match".
