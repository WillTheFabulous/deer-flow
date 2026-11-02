---
name: temu-blanket-title
description: Generate one compliant, traffic-oriented English Temu title from a POD flannel blanket product image. Use when the user provides a blanket product image and asks for a Temu/ecommerce title, especially image-led POD flannel blankets, gift blankets, holiday blankets, quote blankets, animal/floral/abstract pattern blankets, or custom blanket designs.
---

# Temu Blanket Title

## Output

Return exactly one English title and nothing else unless the user asks for analysis.

Hard limits:
- Keep the title under 250 characters; prefer 120-180 characters when possible.
- Use natural American ecommerce English.
- The first 5 words must include `Flannel Blanket`, usually as words 4 and 5.
- Include `280GSM` exactly once in every title, preferably after the first phrase or before the main print description.
- Use only `Flannel` as the explicit material word.
- Do not use `3D`, `2D`, brand/IP terms, unsupported material claims, child/minor terms, liability-heavy claims, or platform residue words.
- Do not promote bed use. Avoid `bed`, `bedding`, `bedspread`, `bedtime`, `night's sleep`, `bedroom`, `bed cover`, or any wording that frames the item as used on a bed or for sleeping overnight.

Read [references/compliance.md](references/compliance.md) before generating titles for exact banned-word and risk rules.
Read [references/holiday-calendar.md](references/holiday-calendar.md) when deciding whether a product is holiday-led.

## Product Facts

Assume the product is a POD flannel blanket:
- Single-sided print.
- Multiple sizes available.
- Machine washable.
- 280GSM, suitable for lightweight warm daily use, nap, sofa, couch, living room, office, travel, car, camping, dorm, and lounge use.

Do not force every product fact into the title. Use product facts only when useful for traffic, clarity, or replacing unsafe/unsupported wording.

## Workflow

### 1. Custom Check

Determine whether the image is a customization product.

If yes:
- The first word of the title must be `Custom`.
- Use custom terms only when the image clearly shows replaceable name, photo, date, custom text, or other buyer-specific personalization.

If no:
- Do not use `Custom` or `Personalized`.

### 2. Lead Type Check

Classify the image:
- `Text-led`: text, message, quote, thank-you note, or love note is the main selling point.
- `Graphic-led`: picture, pattern, animal, floral, object, landscape, or style is the main selling point.
- `Mixed-led`: text/message and graphic form are both important.

Use the lead type to choose the title direction:
- Text-led: prioritize message, quote, letter, love note, thank-you, recipient, or emotional wording.
- Graphic-led: prioritize the main visual subject, pattern, style, and theme.
- Mixed-led: include both the message meaning and the visual form/pattern.

### 3. Holiday Check

Determine whether the image clearly matches an ecommerce holiday in [references/holiday-calendar.md](references/holiday-calendar.md).

If yes:
- Put the holiday name within the first 5 words.

If no:
- Do not force holiday keywords.

### 4. Gift Check

Determine whether the product is gift-led.

If yes:
- Include `Gift` somewhere in the title.
- Do not force `Gift` into the first 5 words when it weakens the core theme plus `Flannel Blanket`.

## First 5 Words

Use the first 5 words as the traffic entry.

Preferred shape:

```text
[Core Theme 1] [Core Theme 2] [Core Attribute] Flannel Blanket
```

Examples:
- `Mother Letter Message Flannel Blanket`
- `Christmas Reindeer Quote Flannel Blanket`
- `Vintage Floral Print Flannel Blanket`
- `Screaming Goat Print Flannel Blanket`
- `Custom Photo Collage Flannel Blanket`
- `Halloween Pumpkin Ghost Flannel Blanket`
- `Astronaut Space Galaxy Flannel Blanket`

Do not start with generic low-value words such as `Soft`, `Comfortable`, `High Quality`, `Warm`, or fixed `1pc Flannel Blanket` unless necessary.
Use `1pc` only when it sounds natural and does not weaken the first 5 keywords.

## Four-Part Title Structure

Build the title in four parts.

1. First 5 words:
Core theme/message/style plus `Flannel Blanket`.

2. Middle theme expansion:
Describe what is printed on the blanket. Use compact phrases such as:
- `... Print`
- `... Pattern`
- `... Design`
- `... Art Print`
- `Printed With ...`
- `Featuring ...`

For text-led or mixed-led products, describe both the visual form and text meaning.
For graphic-led products, describe the main subject and style.

3. Benefits and gift phrase:
Use safe product phrases such as:
- `Soft Warm Machine Washable Blanket`
- `Soft Cozy All Season Blanket`
- `Comfortable Machine Washable Blanket`
- `Gift For Mom`
- `Gift For Dad`
- `Gift For Wife`
- `Gift For Husband`
- `Gift For Family Friends`
- `Holiday Gift`
- `Home Decor Gift`

4. Use scenarios:
Add a fuller concise scene phrase near the end. Prefer covering several high-value scenes when length allows:
- `For Sofa Living Room Office Travel Car`
- `For Couch Living Room Office Travel Car`
- `For Sofa Couch Office Travel Camping Car`
- `For Nap Sofa Living Room Office Travel Car`

Do not use bed-related scenes. Avoid `bed`, `bedding`, `bedspread`, `bedtime`, `night's sleep`, `bedroom`, and `bed cover`.
Do not overload every title with every possible scene, but avoid overly thin scene phrases when the title has room.

## Localized English Check

Before final output:
- Check adjective-before-noun order.
- Keep the main category noun clear.
- Avoid direct Chinese word order.
- Remove repeated meaningless words.
- Fix article errors such as `a Airmail`; use `An` before vowel sounds.
- Prefer compact ecommerce phrasing over full sentences.
- Keep keyword density high but readable.

## Final Check

Before output, confirm:
- Title is under 250 characters.
- `Flannel Blanket` appears within the first 5 words.
- `280GSM` appears exactly once.
- If custom, the first word is `Custom`.
- If holiday-led, the holiday name is within the first 5 words.
- If gift-led, `Gift` appears somewhere.
- No bed-use wording appears, including `bed`, `bedding`, `bedspread`, `bedtime`, `night's sleep`, `bedroom`, and `bed cover`.
- All compliance risks from [references/compliance.md](references/compliance.md) are removed.
