/*
 *  openCook
 *  Copyright (C) 2026 olie.xdev <olie.xdeveloper@googlemail.com>
 *
 *  This program is free software: you can redistribute it and/or modify
 *  it under the terms of the GNU General Public License as published by
 *  the Free Software Foundation, either version 3 of the License, or
 *  (at your option) any later version.
 *
 *  This program is distributed in the hope that it will be useful,
 *  but WITHOUT ANY WARRANTY; without even the implied warranty of
 *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 *  GNU General Public License for more details.
 *
 *  You should have received a copy of the GNU General Public License
 *  along with this program.  If not, see <https://www.gnu.org/licenses/>.
 */

package com.food.opencook.util

import android.content.Context
import androidx.annotation.StringRes
import com.food.opencook.R

/** Helpers for the structured numeric quantities/servings. */
object Numbers {

    /**
     * A Chinese numeral that is a quantity **on its own**, as a regex fragment for embedding in
     * a larger alternation. Supported: the single-value numerals 一/二/两/三/…/十, plus 半 (0.5).
     * Two exclusions, both because a half-read number is worse than none:
     *
     *  - `一` is a morpheme, not the number one, in the fixed vague quantifiers 一点/一些/一起.
     *    Quantity extraction runs *before* noise stripping, so without this guard "一点盐"
     *    would be locked in as "1 salt". `一斤` still matches — the exclusion is those specific
     *    words, not `一` generally.
     *  - Compound / place-value numerals (二十三, 六百) are deliberately unimplemented: a
     *    sequence that touches another numeral or place character (十百千万亿两半) is rejected
     *    whole, so `二十三` yields **no** quantity rather than a wrong 2 or 3.
     */
    const val CN_NUMERAL_REGEX: String =
        "(?<![零一二三四五六七八九十百千万亿两半])" +
            "(?:一(?!点|些|起)|[二两三四五六七八九十]|半)" +
            "(?![零一二三四五六七八九十百千万亿两半])"

    /** Value of a lone Chinese numeral token as matched by [CN_NUMERAL_REGEX] (半→0.5, 两→2,
     *  一→1, 十→10). The vague-quantifier and compound guards live in the regex, so callers
     *  must only pass a token that pattern accepted. */
    fun chineseNumeralValue(token: String): Double? = when (token.trim()) {
        "半" -> 0.5
        "一" -> 1.0
        "二", "两" -> 2.0
        "三" -> 3.0
        "四" -> 4.0
        "五" -> 5.0
        "六" -> 6.0
        "七" -> 7.0
        "八" -> 8.0
        "九" -> 9.0
        "十" -> 10.0
        else -> null
    }

    /**
     * Fold the ASCII-compatible full-width block (U+FF01–U+FF5E) to ASCII — digits `６００` →
     * `600`, comma `，` → `,`, parens `（）` → `()`, plus the ideographic space. CJK recipes
     * often arrive with full-width characters; normalizing before parsing lets the number regex
     * and the unit split see the same shapes they already understand.
     */
    fun normalizeFullWidth(text: String): String {
        if (text.none { it.code in 0xFF01..0xFF5E || it == '　' }) return text
        return buildString(text.length) {
            for (c in text) {
                append(
                    when {
                        c.code in 0xFF01..0xFF5E -> (c.code - 0xFEE0).toChar()
                        c == '　' -> ' '
                        else -> c
                    },
                )
            }
        }
    }

    /** Parse a quantity from text ("400", "1,5", "1.5 ", "两个", "半斤"）→ 400.0 / 1.5 / 2.0 /
     *  0.5; null if none. Full-width digits normalize first, and Chinese numerals are read by
     *  [chineseNumeralValue] — see [CN_NUMERAL_REGEX] for the vague-quantifier guard. */
    fun parseQuantity(text: String?): Double? =
        text
            ?.let { normalizeFullWidth(it).trim().replace(',', '.') }
            ?.let { Regex("""-?\d+(\.\d+)?|$CN_NUMERAL_REGEX""").find(it) }
            ?.let { m -> m.value.toDoubleOrNull() ?: chineseNumeralValue(m.value) }

    /** Render a quantity without a trailing ".0" (400.0 → "400", 1.5 → "1.5"). */
    fun formatQuantity(value: Double?): String? {
        if (value == null) return null
        return if (value % 1.0 == 0.0) value.toLong().toString() else value.toString()
    }

    /** "400 g Nudeln" / "1 Bund Basilikum" / "Salz" — display join of quantity+unit+name.
     *  A CJK amount renders solid ("200克面粉"), the way Chinese writes it — no word spaces. */
    fun displayIngredient(quantity: Double?, unit: String?, name: String): String {
        val parts = listOfNotNull(
            formatQuantity(quantity),
            unit?.takeIf { it.isNotBlank() },
            name.takeIf { it.isNotBlank() },
        )
        val separator = if (parts.any { Scripts.containsCjk(it) }) "" else " "
        return parts.joinToString(separator)
    }

    /**
     * Factor to scale a recipe made for [servings] up/down to [target] people.
     * 1.0 (no scaling) when servings is unknown/zero — we never guess a baseline.
     */
    fun scaleFor(servings: Int?, target: Int): Double =
        if (servings != null && servings > 0 && target > 0) target.toDouble() / servings else 1.0

    /** Scale a quantity by [factor], rounded to 2 decimals. Null (unquantified, e.g. salt) stays null. */
    fun scaleQuantity(quantity: Double?, factor: Double): Double? =
        quantity?.let { Math.round(it * factor * 100.0) / 100.0 }
}

/**
 * Fixed coarse categories the AI assigns; drives meal-plan variety. Stored as
 * language-independent **keys** (e.g. "meat"); the UI shows a localized label.
 * Legacy/AI values in any language are mapped to a key on read via [normalizeKey].
 */
object RecipeCategories {
    /** Stable keys persisted in the DB + sync log. */
    val KEYS = listOf("pasta", "meat", "fish", "soup", "vegetarian", "salad", "dessert", "other")
    const val DEFAULT = "other"

    @StringRes
    fun labelRes(key: String?): Int = when (normalizeKey(key)) {
        "pasta" -> R.string.cat_pasta
        "meat" -> R.string.cat_meat
        "fish" -> R.string.cat_fish
        "soup" -> R.string.cat_soup
        "vegetarian" -> R.string.cat_vegetarian
        "salad" -> R.string.cat_salad
        "dessert" -> R.string.cat_dessert
        else -> R.string.cat_other
    }

    /** German+English fallback aliases, so unit tests and a not-yet-initialized process still
     *  map legacy values. The real list per language lives in `arrays.xml` (`cat_alias_*`) and
     *  is pushed in by `LocalizedLists` as the union over every bundled content language. */
    private val DEFAULT_ALIASES_DE_EN: Map<String, String> = mapOf(
        "nudeln" to "pasta", "noodles" to "pasta",
        "fleisch" to "meat", "geflügel" to "meat",
        "fisch" to "fish",
        "suppe" to "soup", "eintopf" to "soup",
        "vegetarisch" to "vegetarian", "veggie" to "vegetarian", "vegan" to "vegetarian",
        "salat" to "salad",
        "nachtisch" to "dessert", "nachspeise" to "dessert",
        "sonstiges" to "other", "misc" to "other",
    )

    /** Active alias map (word → key). Swapped at runtime by `LocalizedLists`. */
    @Volatile
    private var aliases: Map<String, String> = DEFAULT_ALIASES_DE_EN

    /** Replace the legacy/import alias words for the bundled content languages. */
    fun setAliases(newAliases: Map<String, String>) {
        if (newAliases.isNotEmpty()) aliases = newAliases
    }

    /** Active aliases — exposed so tests can snapshot and restore around [setAliases]. */
    val activeAliases: Map<String, String> get() = aliases

    /** Match a category word (any language) to a stable key, or null when it means nothing to
     *  us. Unlike [normalizeKey] this keeps "I don't know it" apart from a deliberate "other",
     *  which an import needs: a site's `recipeCategory` of "Plat principal"/"Hauptspeise" names
     *  a *meal*, not one of our categories, so it must leave the field empty instead of
     *  silently filing the recipe under [DEFAULT]. */
    fun matchKeyOrNull(raw: String?): String? {
        val t = raw?.trim()?.lowercase()?.takeIf { it.isNotEmpty() } ?: return null
        return if (t in KEYS) t else aliases[t]
    }

    /** Map a stored/legacy/AI category (any language) to a stable key; unknown → [DEFAULT]. */
    fun normalizeKey(raw: String?): String = matchKeyOrNull(raw) ?: DEFAULT

    /** Display label: localized for known/legacy values; a custom free-text value is kept verbatim. */
    fun displayLabel(context: Context, raw: String?): String {
        if (raw.isNullOrBlank()) return context.getString(R.string.cat_other)
        // "Other" is a real answer, not a miss — matchKeyOrNull tells the two apart, so the
        // user's own free text survives instead of collapsing into the "Other" label.
        val key = matchKeyOrNull(raw) ?: return raw.trim()
        return context.getString(labelRes(key))
    }
}

/**
 * Which meals a recipe suits (breakfast/lunch/snack/dinner) — the *when* axis,
 * deliberately separate from [RecipeCategories] (the *what* axis): a soup fits lunch
 * AND dinner, a Hefezopf is baked AND eaten at breakfast/coffee. Multi-value, stored
 * as language-independent keys in a newline-joined TEXT column (the `tags` pattern).
 *
 * `null`/blank storage means [DEFAULT] ("lunch + dinner") — evaluated at read time,
 * never backfilled: sync's MessageApplier rebuilds entities from log columns, so a
 * local-only backfill would be undone by the next sync apply.
 */
object MealTypes {
    /** Stable keys persisted in DB + sync log + AI prompt; order = order of the day. */
    val KEYS = listOf("breakfast", "lunch", "snack", "dinner")

    /** What an unset value means: the classic hot-meal slots. Uniform for all recipes
     *  (deliberately not category-aware) — baked goods are reclassified by hand. */
    val DEFAULT = listOf("lunch", "dinner")

    @StringRes
    fun labelRes(key: String): Int = when (key) {
        "breakfast" -> R.string.mealtype_breakfast
        "lunch" -> R.string.mealtype_lunch
        "snack" -> R.string.mealtype_snack
        else -> R.string.mealtype_dinner
    }

    /** German+English fallback aliases; the per-language list lives in `arrays.xml`
     *  (`mealtype_alias_*`) and is pushed in by `LocalizedLists`. */
    private val DEFAULT_ALIASES_DE_EN: Map<String, String> = mapOf(
        "frühstück" to "breakfast", "fruehstueck" to "breakfast", "morgens" to "breakfast",
        "mittag" to "lunch", "mittagessen" to "lunch",
        "kaffee" to "snack", "kuchen" to "snack", "zwischenmahlzeit" to "snack",
        "abend" to "dinner", "abendessen" to "dinner", "abendbrot" to "dinner",
    )

    /** Active alias map (word → key). Swapped at runtime by `LocalizedLists`. */
    @Volatile
    private var aliases: Map<String, String> = DEFAULT_ALIASES_DE_EN

    /** Replace the AI/import alias words for the bundled content languages. */
    fun setAliases(newAliases: Map<String, String>) {
        if (newAliases.isNotEmpty()) aliases = newAliases
    }

    /** Active aliases — exposed so tests can snapshot and restore around [setAliases]. */
    val activeAliases: Map<String, String> get() = aliases

    /** Map an AI/import value (any language) to a stable key; unknown → null (drop, don't guess). */
    fun normalizeKey(raw: String?): String? {
        val t = raw?.trim()?.lowercase() ?: return null
        if (t in KEYS) return t
        return aliases[t]
    }

    /** Stored column → key list in [KEYS] order; null/blank → [DEFAULT]. */
    fun fromStored(stored: String?): List<String> {
        val keys = stored?.split("\n")?.mapNotNull { normalizeKey(it) }?.distinct().orEmpty()
        return keys.ifEmpty { DEFAULT }.let { list -> KEYS.filter { it in list } }
    }

    /** Key list → stored column; empty → null, so the default semantics apply again. */
    fun toStored(keys: List<String>): String? =
        KEYS.filter { it in keys }.takeIf { it.isNotEmpty() }?.joinToString("\n")
}
