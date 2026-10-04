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


/**
 * Converts between schema.org ISO-8601 durations ("PT25M", "PT1H10M") and a human form
 * ("1 Std 10 Min", "1 h 10 min"). Storage stays ISO-8601; only the UI shows/edits the
 * friendly form.
 *
 * The two directions answer to two different languages, on purpose. What a duration *reads*
 * as is for whoever is holding the phone, and the platform already knows how every language
 * writes an hour — so it is rendered by `android.icu.text.MeasureFormat` in the device locale,
 * the same way [DateLabels] leaves date order to `DateFormat.getBestDateTimePattern`. Nothing
 * to translate, and languages openCook does not ship still come out right.
 *
 * What a duration is *read back* from was typed by a cook or extracted from a recipe, and the
 * platform has no parser for that. So parsing accepts the hour/minute words of **every** bundled
 * language at once (`duration_hours` / `duration_minutes` in arrays.xml) — an English phone must
 * still understand "1 Std 10 Min" out of a German recipe. `LocalizedLists` fills both in.
 *
 * Anything that doesn't look like a duration is passed through unchanged, so user free-text is
 * never destroyed.
 */
object DurationFormat {

    // Accept an optional seconds component too: some recipes (e.g. AI-extracted Japanese
    // ones) carry durations like "PT900S" or "PT0M" instead of "PT15M". Seconds are folded
    // into minutes below so the UI never shows a raw ISO string. See GitHub issue #2.
    private val ISO = Regex("""^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$""", RegexOption.IGNORE_CASE)
    /** German + English, so unit tests and a not-yet-localized process still round-trip.
     *  The real lists live in arrays.xml and arrive via [setUnits]. */
    private val DEFAULT_HOURS = listOf("stunden", "stunde", "std", "h")
    private val DEFAULT_MINUTES = listOf("minuten", "minute", "min", "m")

    @Volatile private var hours = wordRegex(DEFAULT_HOURS)
    @Volatile private var minutes = wordRegex(DEFAULT_MINUTES)
    @Volatile private var inText = textRegex(DEFAULT_HOURS, DEFAULT_MINUTES)
    /** Plain fallback for unit tests and the moments before `LocalizedLists` has run. */
    private val PLAIN_RENDERER: (Int, Int) -> String = { h, m ->
        listOfNotNull(
            h.takeIf { it > 0 }?.let { "$it h" },
            m.takeIf { it > 0 }?.let { "$it min" },
        ).joinToString(" ")
    }

    @Volatile private var renderer: (Int, Int) -> String = PLAIN_RENDERER

    /** Replace the parse vocabulary (called by `LocalizedLists`): the union across every
     *  bundled language, so a recipe written in one is still understood on a phone set to
     *  another. */
    fun setUnits(hourWords: List<String>, minuteWords: List<String>) {
        if (hourWords.isNotEmpty()) hours = wordRegex(hourWords)
        if (minuteWords.isNotEmpty()) minutes = wordRegex(minuteWords)
        inText = textRegex(hourWords.ifEmpty { DEFAULT_HOURS }, minuteWords.ifEmpty { DEFAULT_MINUTES })
    }

    /** Replace how a duration is written out — `LocalizedLists` hands in the platform's own
     *  `MeasureFormat`. Kept behind a lambda so this object stays pure and unit-testable. */
    fun setRenderer(render: (hours: Int, minutes: Int) -> String) { renderer = render }

    /** "25 min" → the 25. Longest word first, so "min" never wins over "minutes" and leaves a
     *  stray "utes" behind, and every word is quoted — a unit may carry a dot ("Std."). The
     *  gap allows a non-breaking space: that is what `MeasureFormat` emits in several
     *  locales, and its own output has to parse back. */
    private fun wordRegex(words: List<String>): Regex = Regex(
        words.filter { it.isNotBlank() }
            .sortedByDescending { it.length }
            .joinToString("|", prefix = """(\d+)[\s\u00A0\u202F]*(?:""", postfix = ")") { Regex.escape(it) },
        RegexOption.IGNORE_CASE,
    )

    /**
     * A cooking time inside running text: "10 Minuten", "1 Std. 30 Min.", "5–7 min",
     * "1,5 hours". Unlike [wordRegex] a unit must end the word — "200 ml" is no minute and
     * "10 Hähnchenschenkel" no hour — and a number is taken whole, so "1,5 Stunden" can't
     * shrink to five hours. A range keeps its first value (look early rather than late).
     * Groups: 1 = hours, 2 = minutes after hours, 3 = minutes alone.
     */
    private fun textRegex(hourWords: List<String>, minuteWords: List<String>): Regex {
        fun alt(words: List<String>) = words.filter { it.isNotBlank() }
            .sortedByDescending { it.length }
            .joinToString("|") { Regex.escape(it) }
        val num = """\d+(?:[.,]\d+)?"""
        val gap = """[\s  ]*"""
        val range = """(?:$gap[-–]$gap$num)?"""
        val h = """(?:${alt(hourWords)})$WORD_BOUNDARY"""
        val m = """(?:${alt(minuteWords)})$WORD_BOUNDARY"""
        return Regex(
            """(?<![\d.,])(?:($num)$range$gap$h(?:\.?$gap($num)$gap$m)?|($num)$range$gap$m)""",
            RegexOption.IGNORE_CASE,
        )
    }

    /** A unit must end a word ("200 ml" is no minute, "10 Hähnchen" no hour) — but CJK text
     *  glues the next word straight on ("10分钟即可"), and Han is a Unicode letter, so only a
     *  *non-CJK* letter disqualifies the match.
     *
     *  Cross-language twin: `server/app/extraction.py`'s `_NON_CJK_LETTER` / `_WORD_END` encode
     *  the same "a unit ends at the first non-CJK letter" boundary for the Python extraction
     *  path (the CJK side there is "anything the positive list does not cover"). The two cannot
     *  share code across languages, so keep them in agreement whenever either's script set
     *  changes. */
    private val WORD_BOUNDARY = """(?![\p{L}&&[^${Scripts.CJK_REGEX_CLASS}]])"""

    /** One cooking time found in a text by [findIn]: where it sits, and how long it is. */
    data class TextDuration(val range: IntRange, val seconds: Int)

    /** The longest timer the platform accepts (`AlarmClock.EXTRA_LENGTH`): 24 hours. */
    const val MAX_TIMER_SECONDS = 24 * 60 * 60

    /**
     * Every cooking time in a recipe step, in order — what the detail screen turns into
     * timer links. Durations outside 1 s..24 h are dropped; nobody sets a timer for those.
     */
    fun findIn(text: String): List<TextDuration> = inText.findAll(text).mapNotNull { match ->
        val g = match.groupValues
        fun value(s: String) = s.replace(',', '.').toDoubleOrNull() ?: 0.0
        val seconds = if (g[1].isNotEmpty()) {
            value(g[1]) * 3600 + (g[2].takeIf { it.isNotEmpty() }?.let(::value) ?: 0.0) * 60
        } else {
            value(g[3]) * 60
        }
        val whole = kotlin.math.round(seconds).toInt()
        if (whole in 1..MAX_TIMER_SECONDS) TextDuration(match.range, whole) else null
    }.toList()

    /** A matched [ISO] duration as total minutes, seconds rounded to the nearest minute. */
    private fun totalMinutes(match: MatchResult): Int {
        val hours = match.groupValues[1].toIntOrNull() ?: 0
        val minutes = match.groupValues[2].toIntOrNull() ?: 0
        val seconds = match.groupValues[3].toIntOrNull() ?: 0
        return (hours * 3600 + minutes * 60 + seconds + 30) / 60
    }

    /** ISO-8601 -> total minutes ("PT1H10M" -> 70, "PT900S" -> 15), or null if not a PT duration. */
    fun minutes(iso: String?): Int? {
        if (iso.isNullOrBlank()) return null
        val match = ISO.matchEntire(iso.trim()) ?: return null
        val total = totalMinutes(match)
        return if (total > 0) total else null
    }

    /** ISO-8601 -> "1 Std 10 Min". Returns the input unchanged if it isn't a PT duration. */
    fun toHuman(iso: String?): String {
        if (iso.isNullOrBlank()) return ""
        val match = ISO.matchEntire(iso.trim()) ?: return iso
        val total = totalMinutes(match)
        val hours = total / 60
        val minutes = total % 60
        // A matched but zero-length duration ("PT0M") shows nothing, not a raw ISO string.
        return if (hours > 0 || minutes > 0) renderer(hours, minutes) else ""
    }

    /**
     * "1 Std 10 Min" -> "PT70M". Returns null for blank input; passes through text
     * that carries no recognisable hours/minutes (stored verbatim).
     */
    fun toIso(text: String?): String? {
        val trimmed = text?.trim().orEmpty()
        if (trimmed.isEmpty()) return null
        if (ISO.matches(trimmed)) return trimmed.uppercase()
        val h = hours.find(trimmed)?.groupValues?.get(1)?.toIntOrNull() ?: 0
        val m = minutes.find(trimmed)?.groupValues?.get(1)?.toIntOrNull() ?: 0
        val total = h * 60 + m
        return if (total > 0) "PT${total}M" else trimmed
    }
}
