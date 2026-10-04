# Adding a language (translating openCook)

openCook is fully localizable by editing plain files. A language is translated in (at most)
**three places**; everything has an English fallback, so a partial translation already works.

There are three independent things to translate:

1. **App UI** — Android string resources (menus, buttons, messages).
2. **App domain word-lists** — grocery-aisle keywords, staples, units, main-protein keywords,
   category/meal aliases and a few grammar helpers (these make the shopping list group correctly,
   keep foreign units, and keep the meal planner varied). Loading these needs **one line of code**
   (registering the language — see §2); nothing else in the app is language-specific.
3. **Server extraction** — the AI prompt + duration words + units + category aliases used when a
   photo is scanned.

openCook currently ships **English (`en`), German (`de`), French (`fr`) and Simplified Chinese
(`zh`)**. The walkthrough below uses **French (`fr`)** as its main example and shows **Chinese
(`zh`)** wherever Chinese differs — it has no plural forms and writes words without spaces.

To add a new language, replace the example code with your language's
[ISO 639-1 code](https://en.wikipedia.org/wiki/List_of_ISO_639-1_codes) (`es`, `it`, `ja`, …).

---

## 1. App UI strings

Copy the default (English) file and translate the **values**, never the `name=` keys:

```
app/src/main/res/values/strings.xml      →  app/src/main/res/values-fr/strings.xml
                                         →  app/src/main/res/values-zh/strings.xml
```

- Translate every `<string>` and `<plurals>` value. Keep placeholders (`%1$s`, `%1$d`, `%%`) and the
  `name=` attributes exactly as-is.
- English plurals have only `one`/`other`; other languages may need more `quantity` forms. Chinese
  is the easy case — it carries only `other`, so every `<plurals>` has a single item.

**Check completeness** with lint — it lists every string you forgot:

```bash
./gradlew lintDebug      # look for "MissingTranslation"
```

Android automatically shows `values-fr/` when the **device UI language** is French (`values-zh/`
for Chinese). Anything you leave out falls back to the English default — nothing breaks.

## 2. App domain word-lists

Same pattern with the arrays file:

```
app/src/main/res/values/arrays.xml       →  app/src/main/res/values-fr/arrays.xml
                                         →  app/src/main/res/values-zh/arrays.xml
```

Translate the items in each list:
- `grocery_kw_*` — keywords that sort an ingredient into a supermarket aisle (substring match,
  lower-case). E.g. for `grocery_kw_meat_fish` add `poulet`, `bœuf`, `poisson`, or `鸡胸`, `牛肉`,
  `三文鱼` in Chinese.
- `ingredient_staples` — background basics the meal planner ignores when scoring (salt, oil, …):
  `sel`, `huile`, … or `盐`, `油`, `酱油`. See the **Staple** term below — this is the one list
  where the Chinese rendering must never drift.
- `protein_kw_*` — main-protein keywords for the meal-planner's variety scoring, so it doesn't
  suggest the same protein twice in a week. The group **keys are fixed** (`protein_kw_poultry`,
  `_fish`, `_mince`, `_pork`, `_beef`, `_lamb`, `_plant`); translate only the keywords, e.g.
  `protein_kw_poultry` → `poulet`, `dinde`, or `鸡胸`, `鸡腿` in Chinese. These deliberately
  overlap with `grocery_kw_meat_fish` but are **sub-grouped** (which protein, not just "is it
  meat") — the variety check needs the distinction so chicken-then-fish counts as variety, not a
  repeat.
- `pantry_defaults` — staples seeded into a new household's pantry (keep display capitalization).
- `ingredient_units` — measuring units in your language (`c. à soupe`, `tasse`, …; `汤匙`, `杯`,
  `斤`). Units are shown **verbatim**, never converted. Multi-word units are matched up to three
  words, so `cuillère à soupe` works — no need to invent a one-word abbreviation. Chinese measure
  words (`个`, `条`, `根`, `片`, `瓣`) and market units (`斤`, `两`) are units too, exactly like
  `piece`/`slice`/`clove`.
- `cat_alias_*` / `mealtype_alias_*` — the words your language uses for the eight recipe
  categories and the four meal types, so a hand-written or imported recipe that says
  `viande` / `dîner` (or `肉类` / `晚餐`) still lands on the `meat` / `dinner` key. The keys
  themselves always match and need no entry. This list is also what a **web import** is matched
  against: a recipe page's `recipeCategory` (`Nachtisch`, `Dessert`, `Soupe`) becomes the recipe's
  category only if it is listed here. A word that names a **meal** rather than a category —
  `Hauptspeise`, `Plat principal` — is deliberately left out: the recipe then stays
  uncategorized (the word is kept as a tag) instead of being filed under `other`. Those
  belong in `mealtype_alias_lunch`/`_dinner`, the *when* axis.

> **Chinese keywords are at least two characters.** Matching is substring-based with no word
> boundary, and a trailing space — the trick that keeps English `oil ` from matching `boiled` —
> does nothing in a language that does not use spaces. A lone `油` would match `油菜`/`油条`/
> `牛油果`, so list the whole word (`食用油`). This trades a little recall for precision, and
> applies to every `grocery_kw_*` and `protein_kw_*` list; units are exempt (they are
> prefix-matched after a number, where a single character is fine).

### Optional: the browser board and cooking times

Three more lists, all optional — leave them out and English is used:

- `discover_sites` — the recipe sites offered as tiles under **Discover**, for households
  cooking in your language. Plain links to each site's own recipe section; tapping one opens it
  in the in-app browser, where the user browses and imports the page they picked. Check that
  every address answers **200**, and prefer sites that publish schema.org JSON-LD — that is what
  the import reads. Without this array your language gets the English board.
- `discover_site_names` — `domain label|display name`, e.g. `cuisineaz|Cuisine AZ`. Only needed
  where plain capitalization gets it wrong; `marmiton` already reads "Marmiton" on its own. This
  names the tile **and** the cookbook an imported recipe is filed under — one list, so the two
  can never drift apart. Merged across all languages.
- `duration_hours` / `duration_minutes` — the words that mark an hour or a minute in a *typed or
  extracted* cooking time (`1 heure 30 minutes`, `45 mn`). Read only. How a duration is **written
  out** needs no translation: the platform's own `MeasureFormat` renders it in the device
  language, so "1 Std. 10 Min." / "1 hr 10 min" come out right even for a language openCook does
  not ship. These are merged across all languages, so a German recipe still parses on an English
  phone.

### Optional: grammar helpers

Everything above is vocabulary. Four more lists describe how your language *builds* ingredient
names — leave them out and the English/German fallback applies, so matching just gets less precise:

- `ingredient_leading_noise` — measure and vague-amount words that leak into a name field
  ("2 c. à soupe sucre", "une pincée de sel"), stripped so the bare noun is left. Only put words
  here that never *start* a real ingredient name — `feuille` would ruin "feuille de laurier".
  Chinese adds its vague quantifiers (`适量`, `少许`, `一点`) and the leaked units, under the same
  rule: `卷` stays out so the `卷` of `卷心菜` is never stripped.
- `ingredient_use_phrases` — words opening a trailing *use* phrase: "huile **pour** la friture"
  (`用于`, `用来`, `用作`) is still oil.
- `ingredient_plural_suffixes` — how a plural is built (`s`, `x` for French; `en`, `n`, `e`, `s`
  for German). Chinese builds no plural, so its list is **empty** — an empty list is safe, the
  suffix pass is simply skipped.
- `ingredient_head_connectors` — **only for head-initial languages.** German and English put the
  head noun last ("schwarzer Pfeffer", "black pepper") and leave this empty; French, Spanish and
  Italian put it first and mark the modifier (`de`, `d'`, `du`, `des`, `à`). With the connector
  listed, a pantry "huile" covers "huile d'olive". Without it, nothing changes. Chinese compounds
  are head-final too (`橄榄油`'s head is `油`), so its list is **empty**.

And two curated lists, `|`-separated per line — keep both **short**, a wrong entry silently
merges two real ingredients:

- `ingredient_synonyms` — same product, different words (`crème fraîche|creme fraiche`,
  `西红柿|番茄`).
- `ingredient_distinctions` — never the same (`lait de coco|lait`). If you filled
  `ingredient_head_connectors`, this is where you stop a staple from swallowing a named variety.
  Chinese needs one line per named oil or vinegar (`橄榄油|油`, `陈醋|醋`), because the bare
  staple is the compound's head.

> **Two syntax rules that are easy to miss:**
> - An item that contains a space, or that must keep a leading/trailing space, has to be
>   **wrapped in double quotes**: `<item>"huile d'olive"</item>`, `<item>"ground "</item>`.
>   An unquoted apostrophe is a build error — inside the quotes it is fine.
> - Keywords are matched as a **substring**, so short words need a trailing space to match as a
>   whole word: `<item>"vin "</item>` (otherwise it also matches *vinaigre*),
>   `<item>"eau "</item>`, `<item>"ail "</item>`.

> **Required — register the code** in `ContentLanguages.CODES`
> (`app/src/main/java/com/food/opencook/data/settings/SettingsRepository.kt`):
> ```kotlin
> val CODES = listOf("en", "de", "fr", "zh")
> ```
> The lists above are loaded as the **union across every registered language**, so a German recipe
> is still classified correctly on an English phone (and vice versa). **Until `fr` is in `CODES`,
> your `values-fr/arrays.xml` is not loaded at all.** Registering here also makes `fr` appear in the
> in-app picker (§4). Anything you leave out falls back to the English array.

## 3. Server extraction (`app/i18n/`)

Copy the English catalog and translate the values:

```
server/app/i18n/en.json                  →  server/app/i18n/fr.json
                                         →  server/app/i18n/zh.json
```

| key | what to do |
|---|---|
| `text_prompt` | translate the extraction instructions (**carefully** — it's engineering text; a wrong instruction can hurt extraction quality) |
| `box_prompt` | translate the dish-photo prompt |
| `duration_hours` / `duration_minutes` | the words that mark hours/minutes (`heure`, `min`, …; `小时`, `分钟`) |
| `units` | units in your language (`克`, `毫升`, `汤匙`, …) |
| `category_aliases` | map your language's category words to the universal keys, e.g. `{"viande": "meat", "poisson": "fish"}` or `{"肉类": "meat", "海鲜": "fish"}` |
| `meal_type_aliases` | map your language's meal words to the universal meal-type keys (`breakfast`, `lunch`, `snack`, `dinner`), e.g. `{"petit-déjeuner": "breakfast", "déjeuner": "lunch", "goûter": "snack", "dîner": "dinner"}` or `{"早餐": "breakfast", "午餐": "lunch", "加餐": "snack", "晚餐": "dinner"}` — used when the model answers in the recipe's language instead of emitting the keys |

`load_i18n("fr")` reads `fr.json`; unknown languages fall back to `en.json`. Units/durations/aliases
are merged with English, so universal tokens (`g`, `ml`, `min`, the category keys) always work even
if you forget one.

## 4. (Optional) Name it in the in-app picker

Once the code is registered in `ContentLanguages.CODES` (§2), it **already appears** under
**Settings → Recipe language** — the picker derives its options from that list. It just shows the
uppercase code (`FR`) until you give it an endonym: add a `lang_french` string to **every**
`values*/strings.xml` and a branch in `contentLanguageLabel()`
(`ui/settings/HouseholdSettingsScreen.kt`):

```kotlin
"fr" -> stringResource(R.string.lang_french)
"zh" -> stringResource(R.string.lang_chinese)
```

The `lang_*` values are **endonyms**: every language file spells them the same way
(`Deutsch`, `English`, `Français`, `中文`) — a list of languages reads best in the languages' own
words, so do *not* translate them into your language. Chinese is shown as plain `中文`, with no
script qualifier.

This is **optional** — on a French device the language is auto-selected (the “Follow system”
default); the label only makes the manual override read nicely.

---

## 5. Chinese (`zh`) specifics

Chinese is the first shipped language that does not put spaces between words, has no plural forms,
and uses its own numerals. Everything below is recorded so the next translator does not have to
re-derive it.

### Terms that must stay consistent (zh ↔ en)

The English column is the domain term; the Chinese column is what actually shipped. Use the Chinese
column for new UI copy and word lists, so the two do not drift apart.

| Term | Chinese | Notes |
|---|---|---|
| Recipe | `菜谱` | not `食谱`, which drifts toward “meal plan” |
| Ingredient | `食材` | |
| Pantry | `食材库存` | |
| Staple | `常备食材` | **never** `主食` — see below. The concept has no UI label (in any language); the `ingredient_staples` list is its data |
| Cookbook | `菜谱集` | the label reads `菜谱集／合集` |
| Household | `家庭` | |
| Meal plan | `饮食计划` | the per-recipe action says `加入周计划` |
| Meal type | `餐次` | stored keys stay `breakfast`/`lunch`/`snack`/`dinner` |
| Content language | `菜谱语言` | the English UI label is “Recipe language”; the field also governs scanning, categories and shopping-list grouping |
| Shopping list | `购物清单` | |

**`Staple` is `常备食材`, never `主食`.** In Chinese, `主食` means rice and noodles — staple
*food*. This project's staples are the background basics the meal planner ignores when working out
what is missing, such as salt and oil. `主食` would tell a Chinese cook the opposite of what the
planner does; the shipped word lists use `常备食材`.

### `zh-TW` falls back to Simplified

Only `zh` is registered, and the only Chinese resource folder is `values-zh/`. A device set to
Traditional Chinese (`zh-TW`, `zh-Hant`) still reports the language code `zh`, which Android
matches against `values-zh/` — so it silently gets the Simplified resources and the `中文` picker
label. This is **recorded, not fixed**: no `zh-Hant` layer is shipped, and the server catalog
(`zh.json`) has the same single-Chinese story.

### Known limits of the Chinese build

- **Compound Chinese numerals are not parsed.** Numerals are read one token at a time (`两个鸡蛋`
  → 2, `半斤五花肉` → 0.5), but place-value compounds such as `二十三` or `六百` are deliberately
  rejected whole and yield **no** quantity rather than a wrong part of one. A dozen-style `一打` is
  not understood as twelve either — only its leading `一` is read, so it comes out as 1. Both are
  rare in recipes and were left out on purpose.
- **The Chinese Discover board is not shipped.** The app falls back to the English board of recipe
  sites, so a Chinese household sees English sites under **Discover**. Browsing and importing still
  work; only the curated site list is English.

---

## How the language is chosen

- **UI language** = the device's system language → `values-<lang>/strings.xml`.
- **Content language** (AI extraction, categories, grocery keywords, staples, units, protein
  keywords) = a **household-wide** setting that defaults to the device language and can be overridden
  in Settings. It drives `server/app/i18n/<lang>.json` (sent with each scan). The app-side word-lists
  in `values-<lang>/arrays.xml` are loaded as the **union of all languages in `ContentLanguages.CODES`**
  (not just the active one), so classification never depends on which single language is active —
  that's why registering the code there is required.
- **Everything falls back to English**, so you can ship a language in stages: translate the UI
  first, the domain lists and the server catalog later.

## Verify

```bash
./gradlew lintDebug          # MissingTranslation / ExtraTranslation / MissingQuantity must be clean
./gradlew testDebugUnitTest  # unit tests
./gradlew assembleDebug      # app builds
cd server && pytest -q       # server (the i18n fallback is covered)
```

`MissingQuantity` is the check a new language usually trips: English `<plurals>` only carry
`one`/`other`, while e.g. French also needs `many` and Polish needs `few`. Lint names the missing
form per language.

Lint cannot see the opposite mistake — a `<string>` you copied over and left in English counts as
translated. **Leave such lines out** instead: the English default is used automatically, and the
gap stays visible to lint and to the next translator.

Then set a device/emulator to your language, scan a recipe in that language, and check that the
shopping list groups its ingredients correctly.

---

## Side note: doing this in Weblate later

The file layout above is already standard, so it can be wired into
[Weblate](https://weblate.org/) without any restructuring — translators then use a web UI instead of
editing files. You'd add **two components** to a Weblate project pointing at this repo:

| Component | Format | File mask | Source |
|---|---|---|---|
| App UI + arrays | Android String Resource | `app/src/main/res/values-*/strings.xml` and `…/arrays.xml` | `values/` |
| Server extraction | JSON file | `server/app/i18n/*.json` | `en.json` |

Gate the server `text_prompt` behind review (it's engineering, not UI copy). Until then, the manual
file-editing process above is all you need.
