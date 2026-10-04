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
 * Script-aware predicates for languages written **without word spaces** (Chinese, Japanese).
 * The parser's word-boundary assumptions hold for Latin/Germanic languages; Han and kana text
 * instead glues unit, number and name into one solid run ("600克面粉"). These helpers let each
 * such assumption relax *only* where these characters are actually present, so English, German
 * and French behaviour is byte-for-byte unchanged. Korean is deliberately excluded: it writes
 * words with spaces, so relaxing a boundary at a space would misfire there.
 */
object Scripts {

    /**
     * The space-less CJK script classes as a regex character-class body, for embedding in `[...]`:
     * `[$CJK_REGEX_CLASS]` matches one such letter. Han covers Chinese (and Japanese kanji),
     * Hiragana/Katakana Japanese kana. Hangul is excluded — Korean uses word spaces.
     */
    const val CJK_REGEX_CLASS = """\p{IsHan}\p{IsHiragana}\p{IsKatakana}"""

    /** True if [c] belongs to a space-less CJK script (Han, Hiragana, Katakana). */
    fun isCjk(c: Char): Boolean = when (Character.UnicodeScript.of(c.code)) {
        Character.UnicodeScript.HAN,
        Character.UnicodeScript.HIRAGANA,
        Character.UnicodeScript.KATAKANA,
        -> true

        else -> false
    }

    /** True if [s] contains at least one CJK character. */
    fun containsCjk(s: String): Boolean = s.any { isCjk(it) }
}
