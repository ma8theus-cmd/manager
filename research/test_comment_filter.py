import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path('/home/matheus/meslibertines_manager_v1')))

from research.comment_research_collector import TARGET, is_acceptable_comment, score_comment


class CommentFilterTests(unittest.TestCase):
    def test_accepts_professional_massage_feedback(self):
        text = "Accueil chaleureux, hygiène irréprochable, massage de qualité et personne très professionnelle."
        self.assertTrue(is_acceptable_comment(text))

    def test_rejects_explicit_or_erotic_feedback(self):
        text = "Très bon accueil mais elle propose une fellation."
        self.assertFalse(is_acceptable_comment(text))

    def test_rejects_comment_without_relevant_service_feedback(self):
        text = "Très jolie femme, magnifique silhouette, j'ai passé un moment incroyable."
        self.assertFalse(is_acceptable_comment(text))

    def test_rejects_generic_service_and_erotic_adjectives(self):
        self.assertFalse(is_acceptable_comment("Service excellent, elle propose un rapport sexuel."))
        self.assertFalse(is_acceptable_comment("Très jolie femme, magnifique silhouette et moment incroyable."))

    def test_accepts_professional_traits_without_word_massage(self):
        text = "Très gentille, souriante et agréable, elle met à l'aise et je recommande."
        self.assertTrue(is_acceptable_comment(text))

    def test_accepts_professional_environment_and_education_feedback(self):
        text = "Personne très bien éduquée, polie et attentionnée. L'ambiance est calme et agréable."
        self.assertTrue(is_acceptable_comment(text))

    def test_accepted_feedback_can_reach_promising_score(self):
        text = "Très gentille, souriante et agréable, elle met à l'aise et je recommande."
        self.assertGreaterEqual(score_comment(text), 8.0)

    def test_collection_stops_at_fifty_qualified_comments(self):
        self.assertEqual(TARGET, 50)

    def test_rejects_less_obvious_sexual_wording(self):
        self.assertTrue(is_acceptable_comment("Massage très agréable, avec une sensualité légère et une personne très douce."))
        self.assertFalse(is_acceptable_comment("Accueil sympathique, puis rapport sexuel et positions très intimes."))
        self.assertFalse(is_acceptable_comment("Hygiène impeccable, mais fellation et orgasme annoncés."))
        self.assertTrue(is_acceptable_comment("Très attentionnée, avec beaucoup de sensualité et un service professionnel."))

    def test_accepts_mild_ambiguity_with_real_service_feedback(self):
        text = "Massage très agréable, accueil chaleureux et personne douce. Une belle rencontre, avec une sensualité légère mais un service très professionnel."
        self.assertTrue(is_acceptable_comment(text))

    def test_rejects_promotional_profile_copy(self):
        text = "Disponible à Lyon, réservez vite, tarifs imbattables et contactez-moi sur Telegram."
        self.assertFalse(is_acceptable_comment(text))


if __name__ == '__main__':
    unittest.main()
