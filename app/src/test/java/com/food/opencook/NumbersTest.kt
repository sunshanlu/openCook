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

import com.food.opencook.util.DurationFormat
import com.food.opencook.util.Numbers
import com.food.opencook.util.RecipeCategories
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class NumbersTest {

    @Test
    fun parseQuantityHandlesCommaAndUnits() {
        assertEquals(400.0, Numbers.parseQuantity("400 g")!!, 0.001)
        assertEquals(1.5, Numbers.parseQuantity("1,5")!!, 0.001)
        assertEquals(2.0, Numbers.parseQuantity("2 Personen")!!, 0.001)
        assertNull(Numbers.parseQuantity("etwas"))
        assertNull(Numbers.parseQuantity(null))
    }

    /** Chinese numerals are read one character at a time: 半 = 0.5, 两 = 2, 十 = 10, 一 = 1.
     *  A vague quantifier must NOT yield a quantity — quantity extraction runs before noise
     *  stripping, so a naive 一→1 would make "一点盐" become "1 salt", worse than not parsing. */
    @Test
    fun parseQuantityReadsChineseNumeralsAndFullWidthDigits() {
        assertEquals(600.0, Numbers.parseQuantity("600克面粉")!!, 0.001)
        assertEquals(2.0, Numbers.parseQuantity("两个鸡蛋")!!, 0.001)
        assertEquals(0.5, Numbers.parseQuantity("半斤五花肉")!!, 0.001)
        assertEquals(1.0, Numbers.parseQuantity("一斤")!!, 0.001) // 一 alone is still one
        assertEquals(10.0, Numbers.parseQuantity("十个")!!, 0.001)

        // The trap: 一 is a morpheme in these fixed vague words, not the number one.
        assertNull(Numbers.parseQuantity("一点盐"))
        assertNull(Numbers.parseQuantity("一些糖"))
        assertNull(Numbers.parseQuantity("一起"))

        // Compound numerals are deliberately unsupported — not half-read as their first digit.
        assertNull(Numbers.parseQuantity("二十三"))
        assertNull(Numbers.parseQuantity("六百"))

        // 一打 ("one dozen") is not expanded to 12 — 打 is a measure word, so only 一 → 1 is read.
        assertEquals(1.0, Numbers.parseQuantity("一打")!!, 0.001)

        // Full-width digits/punctuation normalize before parsing; parens fold to ASCII.
        assertEquals(600.0, Numbers.parseQuantity("６００")!!, 0.001)
        assertEquals(1.5, Numbers.parseQuantity("１，５")!!, 0.001)
        assertEquals(600.0, Numbers.parseQuantity("（６００）")!!, 0.001)
        assertEquals("(600)中筋", Numbers.normalizeFullWidth("（６００）中筋"))
    }

    @Test
    fun formatQuantityDropsTrailingZero() {
        assertEquals("400", Numbers.formatQuantity(400.0))
        assertEquals("1.5", Numbers.formatQuantity(1.5))
        assertNull(Numbers.formatQuantity(null))
    }

    @Test
    fun displayIngredientJoins() {
        assertEquals("400 g Nudeln", Numbers.displayIngredient(400.0, "g", "Nudeln"))
        assertEquals("Salz", Numbers.displayIngredient(null, null, "Salz"))
        assertEquals("1 Bund Basilikum", Numbers.displayIngredient(1.0, "Bund", "Basilikum"))
    }

    /** A CJK amount reads without intervening spaces; Latin amounts keep them. */
    @Test
    fun displayIngredientOmitsSpacesForCjk() {
        assertEquals("200克面粉", Numbers.displayIngredient(200.0, "克", "面粉"))
        assertEquals("1个鸡蛋", Numbers.displayIngredient(1.0, "个", "鸡蛋"))
        assertEquals("400 g Nudeln", Numbers.displayIngredient(400.0, "g", "Nudeln"))
    }

    @Test
    fun scaleForHouseholdSize() {
        assertEquals(2.0, Numbers.scaleFor(servings = 2, target = 4), 0.001) // 2-portion recipe for 4
        assertEquals(0.5, Numbers.scaleFor(servings = 4, target = 2), 0.001)
        assertEquals(1.0, Numbers.scaleFor(servings = null, target = 4), 0.001) // unknown servings → no scaling
        assertEquals(1.0, Numbers.scaleFor(servings = 0, target = 4), 0.001)
    }

    @Test
    fun scaleQuantityRoundsAndKeepsNull() {
        assertEquals(800.0, Numbers.scaleQuantity(400.0, 2.0)!!, 0.001)
        assertEquals(0.33, Numbers.scaleQuantity(1.0, 1.0 / 3.0)!!, 0.001) // rounded to 2 decimals
        assertNull(Numbers.scaleQuantity(null, 2.0)) // unquantified (salt) stays null
    }

    /** An import must tell "I don't know this word" from a deliberate "other", so a site's
     *  free-text `recipeCategory` cannot silently file every recipe under "Other". */
    @Test
    fun categoryMatchKeepsUnknownApartFromOther() {
        assertEquals("dessert", RecipeCategories.matchKeyOrNull("Dessert"))
        assertEquals("dessert", RecipeCategories.matchKeyOrNull(" Nachtisch ")) // alias, trimmed
        assertEquals("other", RecipeCategories.matchKeyOrNull("other")) // a real answer
        assertNull(RecipeCategories.matchKeyOrNull("Hauptspeise")) // a meal, not a category
        assertNull(RecipeCategories.matchKeyOrNull(""))
        assertNull(RecipeCategories.matchKeyOrNull(null))
        // normalizeKey keeps flattening for the storage/filter paths.
        assertEquals("other", RecipeCategories.normalizeKey("Hauptspeise"))
        assertEquals("other", RecipeCategories.normalizeKey(null))
    }

    @Test
    fun durationMinutes() {
        assertEquals(25, DurationFormat.minutes("PT25M"))
        assertEquals(70, DurationFormat.minutes("PT1H10M"))
        assertNull(DurationFormat.minutes(null))
        assertNull(DurationFormat.minutes("nonsense"))
    }
}
