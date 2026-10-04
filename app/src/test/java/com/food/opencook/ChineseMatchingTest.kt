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

package com.food.opencook

import com.food.opencook.data.recipeimport.IngredientLineParser
import com.food.opencook.util.IngredientLexicon
import com.food.opencook.util.IngredientMatch
import com.food.opencook.util.IngredientStaples
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Chinese has no word spaces, so the parser must stop assuming them: a unit glued to the number
 * ("600克面粉"), leading noise glued to the noun ("一点盐"), a solid one-character compound head
 * ("油" in "橄榄油") and full-width punctuation all have to resolve. The vocabulary below is a
 * small Chinese sample pushed straight into the domain singletons — the same seam the French
 * matching test uses — because the real `values-zh/arrays.xml` does not exist yet. The Latin
 * languages must be untouched: that is asserted by the existing English/German/French suites.
 */
class ChineseMatchingTest {

    private val savedVocabulary = IngredientMatch.activeVocabulary
    private val savedStaples = IngredientStaples.ALL
    private val savedPantry = IngredientStaples.DEFAULT_PANTRY
    private val savedSynonyms = IngredientLexicon.activeSynonyms
    private val savedDistinctions = IngredientLexicon.activeDistinctions
    private val savedUnits = IngredientLineParser.activeUnits

    private val chineseUnits = setOf(
        "克", "千克", "毫升", "汤匙", "茶匙", "斤", "两",
        "个", "条", "根", "片", "块", "瓣", "只", "把", "颗",
    )

    @Before
    fun installChineseVocabulary() {
        IngredientLineParser.setUnits(chineseUnits)
        IngredientMatch.setVocabulary(
            IngredientMatch.Vocabulary(
                leadingNoise = savedVocabulary.leadingNoise + listOf("一点", "一些", "适量", "少许"),
                usePhrases = savedVocabulary.usePhrases + listOf("用于", "用来"),
                // Chinese builds plurals by context, not suffixes — the list stays as-is.
                pluralSuffixes = savedVocabulary.pluralSuffixes,
                // Chinese compounds are head-final like German/English: no connectors.
                headConnectors = emptyList(),
            ),
        )
        IngredientStaples.setData(
            all = savedStaples + setOf("油", "盐", "糖", "酱油", "醋", "料酒", "橄榄油"),
            pantry = savedPantry,
        )
        IngredientLexicon.setData(
            synonyms = savedSynonyms,
            distinct = savedDistinctions + listOf("酱油" to "油"),
        )
    }

    @After
    fun restore() {
        IngredientLineParser.setUnits(savedUnits)
        IngredientMatch.setVocabulary(savedVocabulary)
        IngredientStaples.setData(savedStaples, savedPantry)
        IngredientLexicon.setData(savedSynonyms, savedDistinctions)
    }

    private fun p(s: String) = IngredientLineParser.parse(s)

    // --- Fix 1: the unit is prefix-matched, not split on whitespace -----------------------

    @Test
    fun unitGluedToNumberAndNameIsFound() {
        val i = p("600克面粉")
        assertEquals(600.0, i.quantity!!, 1e-9)
        assertEquals("克", i.unit)
        assertEquals("面粉", i.name)
    }

    @Test
    fun longestGluedUnitWins() {
        // "毫升" must win over a hypothetical single-character "毫"; "千克" over "克".
        val i = p("600毫升牛奶")
        assertEquals("毫升", i.unit)
        assertEquals("牛奶", i.name)

        val j = p("2千克土豆")
        assertEquals("千克", j.unit)
        assertEquals("土豆", j.name)
    }

    @Test
    fun measureWordIsAUnit() {
        val i = p("2个鸡蛋")
        assertEquals("个", i.unit)
        assertEquals("鸡蛋", i.name)
    }

    @Test
    fun gluedUnitWithoutANameIsNotTaken() {
        // A unit at the very end leaves no ingredient name — keep the whole rest as the name.
        val i = p("600克")
        assertNull(i.unit)
        assertEquals("克", i.name)
    }

    // --- Fix 2: CJK-boundary noise and use-phrase stripping -------------------------------

    @Test
    fun cjkLeadingNoiseIsStripped() {
        assertEquals("盐", IngredientMatch.normalizeName("一点盐"))
        assertEquals("盐", IngredientMatch.normalizeName("少许盐"))
        assertTrue(IngredientStaples.isStaple("一点盐"))
    }

    @Test
    fun cjkUsePhraseIsStripped() {
        assertEquals("油", IngredientMatch.normalizeName("油用于油炸"))
    }

    // --- Fix 3: a space inside CJK text is not a Latin multi-word phrase ------------------

    @Test
    fun cjkWithSpacesStillMatchesThroughHead() {
        // The "bail on spaces" guard exists for Latin phrases like "sugar snap peas"; CJK text
        // that happens to carry a space is not such a phrase and must keep the head rule.
        assertTrue(IngredientMatch.matches("橄榄 油", "油"))
        // …while the Latin guard is untouched.
        assertFalse(IngredientMatch.matches("sugar snap peas", "sugar"))
    }

    // --- Fix 4: a one-character CJK head is a real head -----------------------------------

    @Test
    fun cjkCompoundHeadMatchesStaple() {
        assertTrue(IngredientMatch.matches("橄榄油", "油"))
        assertTrue(IngredientMatch.covers("油", "橄榄油"))
    }

    @Test
    fun cjkDistinctionBlocksNamedVariety() {
        // "酱油" is soy sauce, not oil — a curated distinction stops the head rule.
        assertFalse(IngredientMatch.matches("酱油", "油"))
        assertFalse(IngredientMatch.covers("油", "酱油"))
    }

    @Test
    fun latinMinimumStemIsUnchanged() {
        // The script-aware minimum must not loosen Latin: "Ei"/"Eis" stays apart.
        assertFalse(IngredientMatch.matches("Ei", "Eis"))
        assertFalse(IngredientMatch.matches("Reis", "Eis"))
    }

    // --- Fix 5: Chinese numerals + full-width digits in a whole ingredient line ------------

    @Test
    fun chineseNumeralLineParses() {
        val j = p("两个鸡蛋")
        assertEquals(2.0, j.quantity!!, 1e-9)
        assertEquals("个", j.unit)
        assertEquals("鸡蛋", j.name)

        val k = p("半斤五花肉")
        assertEquals(0.5, k.quantity!!, 1e-9)
        assertEquals("斤", k.unit)
        assertEquals("五花肉", k.name)
    }

    @Test
    fun vagueQuantifierYieldsNoQuantity() {
        // Quantity extraction runs *before* noise stripping, so the guard must live in the
        // numeral layer; otherwise "一点盐" would be locked in as "1 salt".
        val a = p("一点盐")
        assertNull(a.quantity)
        assertEquals("一点盐", a.name)

        val b = p("一些糖")
        assertNull(b.quantity)
        assertEquals("一些糖", b.name)
    }

    @Test
    fun fullWidthDigitsParseFromALine() {
        val i = p("６００克面粉")
        assertEquals(600.0, i.quantity!!, 1e-9)
        assertEquals("克", i.unit)
        assertEquals("面粉", i.name)
    }

    // --- Fix 6: full-width parentheses are stripped ---------------------------------------

    @Test
    fun fullWidthParenthesesAreStripped() {
        assertEquals("面粉", IngredientMatch.normalizeName("面粉（中筋）"))
        assertEquals("盐", IngredientMatch.normalizeName("盐（细）"))
        // ASCII parentheses keep working.
        assertEquals("面粉", IngredientMatch.normalizeName("面粉 (中筋)"))
    }
}
