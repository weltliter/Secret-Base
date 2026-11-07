"""HTTP setup helper for the synthetic test vault."""
MASTER = "correct horse battery staple"


def initialize(client):
    response = client.post("/api/setup", json={"password": MASTER})
    assert response.status_code == 200
    return response.json()["recovery_key"]
