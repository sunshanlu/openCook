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
 * Script-aware predicates for languages written **without word spaces** (Chinese, Japanese,
 * Korean). The parser's word-boundary assumptions hold for Latin/Germanic languages; CJK text
 * instead glues unit, number and name into one solid run ("600克面粉"). These helpers let each
 * such assumption relax *only* where CJK characters are actually present, so English, German and
 * French behaviour is byte-for-byte unchanged.
 */
object Scripts {

    /**
     * The CJK script classes as a regex character-class body, for embedding in `[...]`:
     * `[$CJK_REGEX_CLASS]` matches one CJK letter. Han covers Chinese (and Japanese kanji),
     * Hiragana/Katakana Japanese kana, Hangul Korean.
     */
    const val CJK_REGEX_CLASS = """\p{IsHan}\p{IsHiragana}\p{IsKatakana}\p{IsHangul}"""

    /** True if [c] belongs to a CJK script. */
    fun isCjk(c: Char): Boolean = when (Character.UnicodeScript.of(c.code)) {
        Character.UnicodeScript.HAN,
        Character.UnicodeScript.HIRAGANA,
        Character.UnicodeScript.KATAKANA,
        Character.UnicodeScript.HANGUL,
        -> true

        else -> false
    }

    /** True if [s] contains at least one CJK character. */
    fun containsCjk(s: String): Boolean = s.any { isCjk(it) }
}
