/** Gaps in jsdom that the components legitimately rely on a browser to provide.
 *
 * jsdom implements the DOM but not layout, so the scrolling APIs are absent. The components use
 * them for real behaviour -- following a new answer when the reader is already at the bottom,
 * and moving to an evidence entry when a citation is pressed -- and stubbing them here is
 * better than a feature check in product code for a method every browser has had for a decade.
 *
 * Deliberately in the test setup rather than in the components: a test environment that cannot
 * scroll should not be a reason for the page to be written as though browsers cannot either.
 */

if (!Element.prototype.scrollTo) {
  Element.prototype.scrollTo = function scrollTo() {
    // Nothing to do. jsdom has no viewport, so there is nowhere to scroll to.
  }
}

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {
    // As above.
  }
}
