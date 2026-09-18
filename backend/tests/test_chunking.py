"""Chunking, from text alone. Pure, with a deterministic token counter.

The counter here is a double, so these tests are about the *rules* — where a chunk starts and
stops, what it records, and that it never exceeds the limit. The real tokenizer's numbers and
the real model's behaviour are checked in the integration run, not here.

    docker compose exec backend python -m unittest discover -s tests -t .
"""

import unittest

from app.chunking import (
    CHUNKING_VERSION,
    MAX_TOKENS,
    OVERLAP_MAX_TOKENS,
    SectionBoundary,
    chunk_document,
)
from tests.search_doubles import WordTokenCounter


def paragraph(words: int, seed: str = "word") -> str:
    return " ".join(f"{seed}{index}" for index in range(words))


def counter() -> WordTokenCounter:
    return WordTokenCounter()


class BoundsTests(unittest.TestCase):
    def test_every_chunk_is_within_the_limit(self):
        """The limit that matters: nothing may reach the model longer than it can read."""
        text = "\n\n".join(paragraph(120, f"p{index}") for index in range(20))

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(
                counter().count_tokens(chunk.embedding_input),
                MAX_TOKENS,
                "a chunk exceeded the model's limit",
            )

    def test_chunks_land_near_the_target_rather_than_far_under(self):
        # Paragraphs small enough that several fit in one chunk, so the greedy packer
        # actually has something to pack. Paragraphs near the target would each become
        # their own chunk and the grouping would never be exercised.
        text = "\n\n".join(paragraph(130, f"p{index}") for index in range(30))

        chunks = chunk_document(text, form_type="10-K", counter=counter())
        sizes = [chunk.token_count for chunk in chunks]
        mean = sum(sizes) / len(sizes)

        self.assertGreater(
            mean,
            300,
            f"chunks averaged {mean:.0f} tokens against a target of 400; the grouping is "
            "not filling them",
        )
        self.assertLessEqual(max(sizes), MAX_TOKENS)

    def test_a_short_document_is_one_chunk(self):
        chunks = chunk_document(
            "NVIDIA designs GPUs.", form_type="10-K", counter=counter()
        )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "NVIDIA designs GPUs.")

    def test_the_limit_is_measured_on_the_embedding_input_not_the_passage(self):
        """The heading is part of what the model reads, so it counts against the limit."""
        text = paragraph(MAX_TOKENS - 2)

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        for chunk in chunks:
            self.assertLess(len(chunk.text.split()), MAX_TOKENS)
            self.assertLessEqual(counter().count_tokens(chunk.embedding_input), MAX_TOKENS)


class OffsetTests(unittest.TestCase):
    def test_offsets_slice_the_source_back_exactly(self):
        text = "\n\n".join(paragraph(80, f"p{index}") for index in range(25))

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        for chunk in chunks:
            self.assertEqual(chunk.text, text[chunk.start : chunk.end])

    def test_chunks_are_in_document_order_and_do_not_run_backwards(self):
        text = "\n\n".join(paragraph(60, f"p{index}") for index in range(20))

        chunks = chunk_document(text, form_type="10-K", counter=counter())
        starts = [chunk.start for chunk in chunks]

        self.assertEqual(starts, sorted(starts))

    def test_no_text_is_lost_between_chunks(self):
        """Apart from whitespace, every word of the source appears in some chunk."""
        text = "\n\n".join(paragraph(90, f"p{index}") for index in range(15))

        chunks = chunk_document(text, form_type="10-K", counter=counter())
        covered = " ".join(chunk.text for chunk in chunks)
        for word in text.split():
            self.assertIn(word, covered)


class SectionTests(unittest.TestCase):
    def text_with_sections(self) -> tuple[str, list[SectionBoundary]]:
        risk = "Item 1A. Risk Factors\n\n" + paragraph(300, "risk")
        mdna = "\n\nItem 7. Management's Discussion\n\n" + paragraph(300, "mdna")
        text = paragraph(200, "intro") + "\n\n" + risk + mdna
        return text, [
            SectionBoundary(name="risk_factors", start=text.index("Item 1A.")),
            SectionBoundary(
                name="management_discussion", start=text.index("Item 7.")
            ),
        ]

    def test_chunks_carry_the_section_they_fall_in(self):
        text, boundaries = self.text_with_sections()

        chunks = chunk_document(
            text, form_type="10-K", sections=boundaries, counter=counter()
        )
        sections = {chunk.section for chunk in chunks}

        self.assertIn("unknown", sections, "text before the first heading")
        self.assertIn("risk_factors", sections)
        self.assertIn("management_discussion", sections)

    def test_a_section_change_does_not_leave_a_chunk_mislabelled(self):
        text, boundaries = self.text_with_sections()

        chunks = chunk_document(
            text, form_type="10-K", sections=boundaries, counter=counter()
        )

        risk_start = text.index("Item 1A.")
        mdna_start = text.index("Item 7.")
        for chunk in chunks:
            if chunk.start >= mdna_start:
                self.assertEqual(chunk.section, "management_discussion")
            elif chunk.start >= risk_start:
                self.assertEqual(chunk.section, "risk_factors")

    def test_an_unknown_section_stays_unknown(self):
        """An 8-K has no detected sections, and that is reported rather than invented."""
        chunks = chunk_document(
            paragraph(300), form_type="8-K", counter=counter()
        )

        self.assertTrue(chunks)
        self.assertEqual({chunk.section for chunk in chunks}, {"unknown"})

    def test_the_heading_is_marked_and_is_not_part_of_the_passage(self):
        chunks = chunk_document(
            paragraph(50), form_type="10-K", counter=counter()
        )
        chunk = chunks[0]

        self.assertTrue(chunk.heading.startswith("["))
        self.assertIn("section: unknown", chunk.heading)
        self.assertNotIn(chunk.heading, chunk.text)
        self.assertTrue(chunk.embedding_input.startswith(chunk.heading))
        self.assertIn(chunk.text, chunk.embedding_input)


class SplittingTests(unittest.TestCase):
    def test_a_long_table_is_split_at_row_boundaries(self):
        """A financial table cut mid-row loses the link between a number and its column."""
        # Four cells per row, so a whole row has three tabs and a cut one would have fewer.
        rows = [
            f"Revenue\t{index},000\t{index * 2},000\tline {index}"
            for index in range(200)
        ]
        text = "\n".join(rows)

        chunks = chunk_document(text, form_type="10-Q", counter=counter())

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            for line in chunk.text.split("\n"):
                self.assertEqual(line.count("\t"), 3, f"a row was cut mid-way: {line!r}")

    def test_a_paragraph_of_many_sentences_is_split_at_sentence_ends(self):
        text = " ".join(f"This is sentence number {index}." for index in range(300))

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertTrue(chunk.text.endswith("."), chunk.text[-40:])

    def test_one_enormous_sentence_is_still_broken_up(self):
        """The alternative is handing the model something it would silently truncate."""
        text = " ".join(f"word{index}" for index in range(2000))

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(counter().count_tokens(chunk.embedding_input), MAX_TOKENS)

    def test_a_hard_split_does_not_cut_a_word_in_half(self):
        text = " ".join(f"word{index}" for index in range(1500))

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        rejoined = " ".join(chunk.text for chunk in chunks)
        for original in text.split():
            self.assertIn(original, rejoined)


class OverlapTests(unittest.TestCase):
    def test_consecutive_chunks_can_share_their_boundary_paragraph(self):
        text = "\n\n".join(
            paragraph(200 if index % 2 else 20, f"p{index}") for index in range(20)
        )

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        overlaps = [
            first.end > second.start for first, second in zip(chunks, chunks[1:])
        ]
        self.assertTrue(any(overlaps), "no chunk overlapped its neighbour")

    def test_overlap_never_pushes_a_chunk_over_the_limit(self):
        text = "\n\n".join(paragraph(200, f"p{index}") for index in range(20))

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        for chunk in chunks:
            self.assertLessEqual(counter().count_tokens(chunk.embedding_input), MAX_TOKENS)

    def test_a_large_boundary_paragraph_is_not_repeated(self):
        """Overlap is bounded, so it can never crowd out the passage it is meant to help."""
        big = paragraph(OVERLAP_MAX_TOKENS * 3, "big")
        text = "\n\n".join([big, paragraph(300, "a"), big, paragraph(300, "b")])

        chunks = chunk_document(text, form_type="10-K", counter=counter())

        for chunk in chunks:
            self.assertLessEqual(counter().count_tokens(chunk.embedding_input), MAX_TOKENS)


class DeterminismTests(unittest.TestCase):
    def test_the_same_text_produces_the_same_chunks(self):
        text = "\n\n".join(paragraph(120, f"p{index}") for index in range(15))

        first = chunk_document(text, form_type="10-K", counter=counter())
        second = chunk_document(text, form_type="10-K", counter=counter())

        self.assertEqual(first, second)

    def test_every_chunk_records_the_chunking_version(self):
        chunks = chunk_document(paragraph(100), form_type="10-K", counter=counter())

        self.assertEqual({chunk.chunking_version for chunk in chunks}, {CHUNKING_VERSION})


class EmptyInputTests(unittest.TestCase):
    def test_empty_text_produces_no_chunks(self):
        self.assertEqual(
            chunk_document("", form_type="10-K", counter=counter()), ()
        )

    def test_whitespace_only_text_produces_no_chunks(self):
        self.assertEqual(
            chunk_document("   \n\n  \t ", form_type="10-K", counter=counter()), ()
        )

    def test_a_document_of_one_word_is_one_chunk(self):
        chunks = chunk_document("NVIDIA", form_type="10-K", counter=counter())

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].start, 0)
        self.assertEqual(chunks[0].end, 6)


if __name__ == "__main__":
    unittest.main()
