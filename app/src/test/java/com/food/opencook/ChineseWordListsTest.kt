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
import com.food.opencook.data.settings.ContentLanguages
import com.food.opencook.util.GroceryCategories
import com.food.opencook.util.GroceryCategory
import com.food.opencook.util.IngredientLexicon
import com.food.opencook.util.IngredientMatch
import com.food.opencook.util.IngredientStaples
import com.food.opencook.util.ProteinGroups
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * The Chinese domain word lists (`values-zh/arrays.xml`). A JVM unit test cannot read Android
 * resources, so — like [SharedVectorsTest] reads its vectors — this test reads the resource file
 * straight off disk and pushes the *real* items into the same domain singletons `LocalizedLists`
 * fills at runtime. That keeps the test honest: it fails if the file holds a keyword that is a
 * single character, if a staple goes missing, or if a chosen keyword fires on the wrong
 * ingredient. The matcher mechanics are covered by [ChineseMatchingTest]; this covers the data.
 */
class ChineseWordListsTest {

    private val savedVocabulary = IngredientMatch.activeVocabulary
    private val savedStaples = IngredientStaples.ALL
    private val savedPantry = IngredientStaples.DEFAULT_PANTRY
    private val savedSynonyms = IngredientLexicon.activeSynonyms
    private val savedDistinctions = IngredientLexicon.activeDistinctions
    private val savedUnits = IngredientLineParser.activeUnits
    private val savedRules = GroceryCategories.activeRules
    private val savedGroups = ProteinGroups.activeGroups

    /** The resource file, parsed to `name -> items`, read from disk (user.dir → walk up). */
    private val arrays: Map<String, List<String>> = parseArrays(findArrays())

    private fun items(name: String): List<String> = arrays[name].orEmpty()

    // --- data policy ---------------------------------------------------------------------

    /** Substring matching has no word boundary in Chinese, so a one-character keyword would
     *  fire on unrelated ingredients. Grocery-aisle and protein keywords are substring-matched. */
    @Test
    fun substringKeywordsAreNeverASingleCharacter() {
        val substringArrays = arrays.keys.filter {
            it.startsWith("grocery_kw_") || it.startsWith("protein_kw_")
        }
        assertTrue("no grocery/protein arrays parsed", substringArrays.isNotEmpty())
        substringArrays.forEach { key ->
            items(key).forEach { word ->
                assertTrue("$key has a single-character keyword: '$word'", word.length >= 2)
            }
        }
    }

    @Test
    fun bareBackgroundBasicsAreStaples() {
        val staples = items("ingredient_staples").toSet()
        listOf("盐", "油", "糖", "酱油", "醋", "料酒").forEach {
            assertTrue("staple '$it' missing", it in staples)
        }
    }

    /** The ticket's example: the bare `油` must not swallow the named variety. */
    @Test
    fun namedVarietiesAreDistinguishedFromTheBareWord() {
        val pairs = items("ingredient_distinctions").map { it.split("|") }
        assertTrue("橄榄油|油 distinction missing", pairs.any { it.containsAll(listOf("橄榄油", "油")) })
        assertTrue("酱油|油 distinction missing", pairs.any { it.containsAll(listOf("酱油", "油")) })
        assertTrue("椰奶|牛奶 distinction missing", pairs.any { it.containsAll(listOf("椰奶", "牛奶")) })
    }

    @Test
    fun headConnectorsAndPluralSuffixesContributeNothing() {
        assertTrue(items("ingredient_head_connectors").isEmpty())
        assertTrue(items("ingredient_plural_suffixes").isEmpty())
    }

    @Test
    fun languageIsRegistered() {
        assertTrue("zh must be registered in ContentLanguages.CODES", "zh" in ContentLanguages.CODES)
    }

    // --- the recipes/shopping-list behaviour the lists buy -------------------------------

    @Before
    fun installChineseVocabulary() {
        GroceryCategories.setRules(
            listOf(
                GroceryCategory.FROZEN to items("grocery_kw_frozen"),
                GroceryCategory.MEAT_FISH to items("grocery_kw_meat_fish"),
                GroceryCategory.PRODUCE to items("grocery_kw_produce"),
                GroceryCategory.BAKERY to items("grocery_kw_bakery"),
                GroceryCategory.DRINKS to items("grocery_kw_drinks"),
                GroceryCategory.SPICES to items("grocery_kw_spices"),
                GroceryCategory.PANTRY to items("grocery_kw_pantry"),
                GroceryCategory.DAIRY to items("grocery_kw_dairy"),
            ),
        )
        ProteinGroups.setGroups(
            listOf(
                "geflügel" to items("protein_kw_poultry"),
                "fisch" to items("protein_kw_fish"),
                "hackfleisch" to items("protein_kw_mince"),
                "schwein" to items("protein_kw_pork"),
                "rind" to items("protein_kw_beef"),
                "lamm" to items("protein_kw_lamb"),
                "tofu" to items("protein_kw_plant"),
            ),
        )
        IngredientStaples.setData(all = items("ingredient_staples").toSet(), pantry = savedPantry)
        IngredientLineParser.setUnits(items("ingredient_units").toSet())
        IngredientMatch.setVocabulary(
            IngredientMatch.Vocabulary(
                leadingNoise = items("ingredient_leading_noise"),
                usePhrases = items("ingredient_use_phrases"),
                pluralSuffixes = items("ingredient_plural_suffixes"),
                headConnectors = items("ingredient_head_connectors"),
            ),
        )
        IngredientLexicon.setData(
            synonyms = items("ingredient_synonyms").map { it.split("|").toSet() },
            distinct = items("ingredient_distinctions").mapNotNull {
                val p = it.split("|")
                if (p.size == 2) p[0] to p[1] else null
            },
        )
    }

    @After
    fun restore() {
        GroceryCategories.setRules(savedRules)
        ProteinGroups.setGroups(savedGroups)
        IngredientStaples.setData(savedStaples, savedPantry)
        IngredientLineParser.setUnits(savedUnits)
        IngredientMatch.setVocabulary(savedVocabulary)
        IngredientLexicon.setData(savedSynonyms, savedDistinctions)
    }

    @Test
    fun ingredientsLandInTheRightAisle() {
        assertEquals(GroceryCategory.MEAT_FISH, GroceryCategories.categorize("猪肉"))
        assertEquals(GroceryCategory.MEAT_FISH, GroceryCategories.categorize("三文鱼"))
        assertEquals(GroceryCategory.PRODUCE, GroceryCategories.categorize("西红柿"))
        assertEquals(GroceryCategory.PRODUCE, GroceryCategories.categorize("西兰花"))
        assertEquals(GroceryCategory.BAKERY, GroceryCategories.categorize("面包"))
        assertEquals(GroceryCategory.PANTRY, GroceryCategories.categorize("意面"))
        assertEquals(GroceryCategory.PANTRY, GroceryCategories.categorize("白糖"))
        assertEquals(GroceryCategory.SPICES, GroceryCategories.categorize("食盐"))
        assertEquals(GroceryCategory.DAIRY, GroceryCategories.categorize("牛奶"))
        assertEquals(GroceryCategory.DAIRY, GroceryCategories.categorize("鸡蛋"))
        // 椰奶 is coconut milk, not dairy — PANTRY is checked before DAIRY.
        assertEquals(GroceryCategory.PANTRY, GroceryCategories.categorize("椰奶"))
    }

    /** An egg must not be read as chicken just because 鸡 is a morpheme in 鸡蛋. */
    @Test
    fun eggIsNotPoultryAndMilkIsNotBeef() {
        assertNull(ProteinGroups.groupOf("鸡蛋"))
        assertNull(ProteinGroups.groupOf("牛奶"))
        assertNull(ProteinGroups.groupOf("西红柿"))
    }

    @Test
    fun weekPlannerCountsChickenFishAsVariety() {
        // Two different poultry-/fish-group dishes are variety; chicken twice is a repeat.
        assertEquals("geflügel", ProteinGroups.groupOf("鸡胸肉"))
        assertEquals("fisch", ProteinGroups.groupOf("三文鱼"))
        assertEquals("rind", ProteinGroups.groupOf("牛肉"))
        assertEquals("schwein", ProteinGroups.groupOf("猪肉"))
        assertEquals("lamm", ProteinGroups.groupOf("羊肉"))
        assertEquals("hackfleisch", ProteinGroups.groupOf("肉末"))
        assertEquals("tofu", ProteinGroups.groupOf("豆腐"))
    }

    @Test
    fun bareBasicsAreIgnoredByThePlanner() {
        listOf("盐", "油", "糖", "酱油", "醋", "料酒").forEach {
            assertTrue("'$it' should be a staple", IngredientStaples.isStaple(it))
        }
        // Vague-amount words are stripped before the staple test.
        assertTrue(IngredientStaples.isStaple("一点盐"))
        assertTrue(IngredientStaples.isStaple("少许糖"))
    }

    @Test
    fun bareOilDoesNotCoverNamedVarieties() {
        assertFalse(IngredientMatch.covers("油", "橄榄油"))
        assertFalse(IngredientMatch.covers("油", "酱油"))
        assertFalse(IngredientMatch.covers("糖", "红糖"))
        assertFalse(IngredientMatch.covers("醋", "陈醋"))
        // 白糖 is plain white sugar, so the bare 糖 *does* cover it — not a distinction.
        assertTrue(IngredientMatch.covers("糖", "白糖"))
        assertTrue(IngredientMatch.covers("面粉", "高筋面粉"))
    }

    @Test
    fun twoWordingsOfOneIngredientMerge() {
        assertTrue(IngredientMatch.matches("西红柿", "番茄"))
        assertTrue(IngredientMatch.matches("土豆", "马铃薯"))
        assertTrue(IngredientMatch.matches("香油", "芝麻油"))
    }

    @Test
    fun genuinelyDifferentIngredientsStayApart() {
        assertFalse(IngredientMatch.matches("椰奶", "牛奶"))
        assertFalse(IngredientMatch.matches("豆奶", "牛奶"))
        assertFalse(IngredientMatch.covers("牛奶", "椰奶"))
    }

    @Test
    fun unitsStayVerbatimIncludingMarketAndMeasureWords() {
        val i = IngredientLineParser.parse("600克面粉")
        assertEquals(600.0, i.quantity!!, 1e-9)
        assertEquals("克", i.unit)
        assertEquals("面粉", i.name)

        val j = IngredientLineParser.parse("两个鸡蛋")
        assertEquals(2.0, j.quantity!!, 1e-9)
        assertEquals("个", j.unit)
        assertEquals("鸡蛋", j.name)

        val k = IngredientLineParser.parse("半斤五花肉")
        assertEquals(0.5, k.quantity!!, 1e-9)
        assertEquals("斤", k.unit)
        assertEquals("五花肉", k.name)

        // ASCII quantity in front, because the numeral layer deliberately refuses 二两/一两
        // as a compound numeral — 两 only becomes a unit here.
        val l = IngredientLineParser.parse("1两肉")
        assertEquals(1.0, l.quantity!!, 1e-9)
        assertEquals("两", l.unit)
        assertEquals("肉", l.name)
    }

    // --- tiny resource-file reader --------------------------------------------------------

    /** Walk up from the test's working directory to the module's resource file. */
    private fun findArrays(): File {
        var dir: File? = File(System.getProperty("user.dir"))
        repeat(6) {
            val f = File(dir, "app/src/main/res/values-zh/arrays.xml")
            if (f.exists()) return f
            val g = File(dir, "src/main/res/values-zh/arrays.xml")
            if (g.exists()) return g
            dir = dir?.parentFile
        }
        error("values-zh/arrays.xml not found from ${System.getProperty("user.dir")}")
    }

    private fun parseArrays(file: File): Map<String, List<String>> {
        val xml = file.readText()
        val array = Regex("""<string-array\s+name="([^"]+)"\s*(?:/>|>(.*?)</string-array>)""", RegexOption.DOT_MATCHES_ALL)
        val item = Regex("""<item>(.*?)</item>""", RegexOption.DOT_MATCHES_ALL)
        return array.findAll(xml).associate { m ->
            val body = m.groupValues[2]
            m.groupValues[1] to item.findAll(body).map { it.groupValues[1].trim() }.toList()
        }
    }
}
