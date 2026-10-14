.PHONY: up down reset logs ps demo test dev scale

up:            ## Construire et démarrer tout l'écosystème
	docker compose up -d --build

down:          ## Arrêter (les données sont conservées)
	docker compose down

reset:         ## Arrêter et effacer toutes les données
	docker compose down -v

logs:          ## Suivre les journaux de tous les services
	docker compose logs -f --tail=50

ps:            ## État et santé des conteneurs
	docker compose ps

demo:          ## Jouer le scénario de démonstration
	./scripts/demo.sh

scale:         ## Exemple de mise à l'échelle horizontale
	docker compose up -d --scale order-service=3 --scale stock-service=2

test:          ## Tests unitaires et de bout en bout (sans Docker)
	python -m pytest -q

dev:           ## Mode démonstration sans Docker (un seul processus)
	python scripts/dev_server.py
