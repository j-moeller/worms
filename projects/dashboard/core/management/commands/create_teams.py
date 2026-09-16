import random

from core.models.competition import ApplicationSetting, Competition
from core.models.team import Team
from django.core.management.base import BaseCommand
from django.db import transaction

ADJECTIVES = [
    "Algorithmic",
    "Computational",
    "Programmatic",
    "Syntactic",
    "Semantic",
    "Recursive",
    "Distributed",
    "Scalable",
    "Efficient",
    "Deterministic",
    "Probabilistic",
    "Stochastic",
    "Heuristic",
    "Automated",
    "Adaptive",
    "Predictive",
    "Generative",
    "Supervised",
    "Unsupervised",
    "Reinforcement",
    "Neural",
    "Statistical",
    "Deep",
    "Latent",
    "Contextual",
    "Adversarial",
    "Robust",
    "Secure",
    "Encrypted",
    "Cryptographic",
    "Authenticated",
    "Authorized",
    "Defensive",
    "Offensive",
    "Penetration",
    "Exploitative",
    "Vulnerable",
    "Hardened",
    "Resilient",
    "Forensic",
    "Stealthy",
    "Intrusive",
    "Malicious",
    "Ethical",
    "Covert",
    "Sandboxed",
    "Isolated",
    "Networked",
    "Binary",
    "Low-level",
    "High-level",
    "Open-source",
    "Modular",
    "Concurrent",
    "Parallel",
    "Real-time",
    "AI",
]

NOUNS = [
    # Machine learning / AI
    "Gradient",
    "Tool-Caller",
    "Skynet",
    "T-1000",
    "Strategist",
    "Trainer",
    "Classifier",
    "Predictor",
    "Estimator",
    "Optimizer",
    "Annotator",
    "Detector",
    "Tokenizer",
    "Transformer",
    "Distiller",
    # Security / hacking
    "Intruder",
    "Cracker",
    "Spoofer",
    "Sniffer",
    "Scanner",
    "Botmaster",
    "Operator",
    "Forger",
    "Phisher",
    # Infrastructure / systems
    "Orchestrator" "Breacher",
    "Proxy",
    "Logger",
]


class Command(BaseCommand):
    help = "Create random teams for the current competition"

    def add_arguments(self, parser):
        parser.add_argument("n_teams", type=int)
        parser.add_argument("--seed", type=int, default=1)

    def handle(self, n_teams, seed, *args, **options):
        try:
            settings = ApplicationSetting.objects.select_related(
                "current_competition"
            ).get(key="current")
            competition = settings.current_competition
        except ApplicationSetting.DoesNotExist:
            print("Do you want to create a competition? [Y/n]")
            c = input()
            if c.strip() != "" and c.lower() != "y":
                raise
            print("Competition name:")
            name = input()

            with transaction.atomic():
                competition = Competition(name=name)
                competition.save()
                settings = ApplicationSetting(
                    key="current", current_competition=competition
                )
                settings.save()

        n_existing_teams = len(Team.objects.filter(competition=competition))
        if n_existing_teams > 0:
            print(f"{n_existing_teams} teams already exist. Continue? [N/y]")
            c = input()
            if c.strip() == "" or c.lower() != "y":
                print("Exit. No team created")
                return

        rng = random.Random(seed)

        adjectives = rng.choices(ADJECTIVES, k=n_teams)
        nouns = rng.choices(NOUNS, k=n_teams)

        team_names = [f"{a} {b}" for a, b in zip(adjectives, nouns)]

        with transaction.atomic():
            for team_name in team_names:
                team = Team(
                    name=team_name,
                    competition=competition,
                )
                team.save()
