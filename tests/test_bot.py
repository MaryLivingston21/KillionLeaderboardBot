import unittest

from bot import parse_score
from bot import START_DATE
from bot import START_PUZZLE
from datetime import date


class TestScoreParser(unittest.TestCase):

    # def test_manual_input(self):
    #     user_input = input(
    #         "\nEnter a score submission to test "
    #         "(example: Krillion #41 415): "
    #     )
    #
    #     result = parse_score(user_input)
    #
    #     print(f"Parsed result: {result}")
    #
    #     self.assertIsNotNone(
    #         result,
    #         "The input was not recognized as a valid submission."
    #     )

    def test_valid_submission(self):
        result = parse_score("Krillion #41 🦐 415 🏮🏮🏮🤡🐟🐟🏮")

        self.assertEqual((41, 415), result)


    def test_another_valid_submission(self):
        result = parse_score("Krillion #40 🦐 335 🐟🦑🫧🦑🦑🐟🏮")

        self.assertEqual((40, 335), result)


    def test_score_zero(self):
        result = parse_score("Krillion #41 🦐 0 🏮🏮🏮🤡🐟🐟🏮")

        self.assertEqual((41, 0), result)


    def test_large_numbers(self):
        result = parse_score("Krillion #9999 🦐 123456 🐟🦑🫧🦑🦑🐟🏮")

        self.assertEqual((9999, 123456), result)


    def test_missing_puzzle(self):
        result = parse_score("Krillion 🦐 415 🐟🦑🫧🦑🦑🐟🏮")

        self.assertIsNone(result)

    def test_missing_shrimp_emoji(self):
        result = parse_score("Krillion #100 532 🐟🦑🫧🦑🦑🐟🏮")

        self.assertEqual(None, result)

    def test_missing_score(self):
        result = parse_score("Krillion #41 🦐 🐟🦑🫧🦑🦑🐟🏮")

        self.assertEqual(None, result)


    def test_random_message(self):
        result = parse_score("Hello everyone!")

        self.assertEqual(None, result)


def get_puzzle_for_date(test_date):
    days_since_start = (test_date - START_DATE).days

    return START_PUZZLE + days_since_start


def print_test_leaderboard(test_date=None):
    # Default to August 25, 2026 for testing
    if test_date is None:
        test_date = date(2026, 8, 25)

    puzzle = get_puzzle_for_date(test_date)

    submissions = [
        ("Human", puzzle, 415),
        ("Alice", puzzle, 427),
        ("Bob", puzzle, 389),
        ("Charlie", puzzle, 451),
        ("Dave", puzzle, 402),
    ]

    # Sort by score, lowest first
    leaderboard = sorted(
        submissions,
        key=lambda x: x[2]
    )

    print("\n")
    print(f"🏆 PUZZLE #{puzzle} LEADERBOARD")
    print(f"📅 {test_date.strftime('%B %d, %Y')}")
    print("--------------------------")

    medals = ["🥇", "🥈", "🥉"]

    for i, (username, puzzle, score) in enumerate(leaderboard):

        if i < 3:
            prefix = medals[i]
        else:
            prefix = f"{i + 1}."

        print(f"{prefix} {username} — {score}")

    print("--------------------------")


if __name__ == "__main__":
    unittest.main(exit=False)
    print_test_leaderboard()
