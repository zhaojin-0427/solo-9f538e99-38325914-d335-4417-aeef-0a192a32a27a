"""F- prefixed business numbers must never resolve through internal row ids."""

import pytest


def _formula_map(client):
    rows = client.get("/catalog").json()["formulas"]
    return {row["formula_code"]: row for row in rows}


def test_f001_does_not_overwrite_f100_with_internal_id_one(client):
    # F-100 is the first formula row, so its internal id is 1.
    response = client.post(
        "/batches",
        json={
            "fields": [
                {"name": "p"},
                {"name": "h"},
                {"name": "e"},
                {"name": "x"},
                {"name": "y"},
            ],
            "formulas": [
                {
                    "formula_no": "F-100",
                    "result": "e",
                    "left": "p",
                    "operator": "*",
                    "right": "h",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    created = response.json()["applied"]["formulas"][0]
    assert created["formula_code"] == "F-100"
    assert created["id"] == 1
    f100_version = created["version"]

    # Registering F-001 must create a second row instead of overwriting F-100.
    response = client.post(
        "/formulas",
        json={
            "formula_no": "F-001",
            "result": "y",
            "left": "x",
            "operator": "/",
            "right": "p",
        },
    )
    assert response.status_code == 200, response.text
    new_row = response.json()
    assert new_row["formula_code"] == "F-001"
    assert new_row["id"] != 1

    catalog = _formula_map(client)
    assert set(catalog) == {"F-001", "F-100"}
    assert catalog["F-100"]["result"] == "e"
    assert catalog["F-100"]["left"] == "p"
    assert catalog["F-100"]["operator"] == "*"
    assert catalog["F-100"]["right"] == "h"
    assert catalog["F-100"]["version"] == f100_version
    assert catalog["F-001"]["result"] == "y"
    assert catalog["F-001"]["operator"] == "/"


def test_withdraw_f001_only_withdraws_f001(client):
    client.post(
        "/formulas",
        json={
            "formula_no": "F-100",
            "result": "e",
            "left": "p",
            "operator": "*",
            "right": "h",
        },
    )
    client.post(
        "/formulas",
        json={
            "formula_no": "F-001",
            "result": "y",
            "left": "x",
            "operator": "/",
            "right": "p",
        },
    )

    response = client.post("/formulas/F-001/withdraw")
    assert response.status_code == 200, response.text
    withdrawn = response.json()
    assert withdrawn["formula_code"] == "F-001"
    assert withdrawn["active"] is False

    catalog = _formula_map(client)
    assert catalog["F-001"]["active"] is False
    assert catalog["F-100"]["active"] is True

    # Withdrawing a non-existent F- code reports not-found and changes nothing.
    response = client.post("/formulas/F-999/withdraw")
    assert response.status_code == 404
    catalog = _formula_map(client)
    assert catalog["F-100"]["active"] is True
    assert catalog["F-001"]["active"] is False


def test_withdraw_unknown_f_code_returns_404(client):
    response = client.delete("/formulas/F-042")
    assert response.status_code == 404
    assert "找不到" in response.json()["detail"] or "不存在" in response.json()["detail"]


def test_corrections_stay_on_their_own_f_number(client):
    client.post(
        "/formulas",
        json={
            "formula_no": "F-100",
            "result": "e",
            "left": "p",
            "operator": "*",
            "right": "h",
        },
    )
    client.post(
        "/formulas",
        json={
            "formula_no": "F-001",
            "result": "y",
            "left": "x",
            "operator": "/",
            "right": "p",
        },
    )

    response = client.put(
        "/formulas/F-001",
        json={"result": "y", "left": "p", "operator": "/", "right": "x"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["formula_code"] == "F-001"

    catalog = _formula_map(client)
    assert catalog["F-001"]["left"] == "p"
    assert catalog["F-001"]["right"] == "x"
    assert catalog["F-001"]["version"] == 2
    # F-100 is untouched.
    assert catalog["F-100"]["operator"] == "*"
    assert catalog["F-100"]["version"] == 1


def test_bare_numeric_reference_keeps_legacy_id_lookup(client):
    response = client.post(
        "/formulas",
        json={
            "formula_no": "F-100",
            "result": "e",
            "left": "p",
            "operator": "*",
            "right": "h",
        },
    )
    internal_id = response.json()["id"]
    assert internal_id == 1

    # A bare number still resolves via business number then internal id.
    response = client.post(
        "/formulas",
        json={
            "formula_no": 1,
            "result": "e",
            "left": "p",
            "operator": "/",
            "right": "h",
        },
    )
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["id"] == internal_id
    assert row["formula_code"] == "F-100"
    assert row["operator"] == "/"
    assert _formula_map(client)["F-100"]["version"] == 2


def test_expression_prefix_is_treated_as_f_business_code(client):
    response = client.post(
        "/batches",
        json={
            "formulas": [
                {"formula_no": "F-100", "expression": "e = p * h"},
                {"expression": "F-001: y = x / p"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    codes = {row["formula_code"] for row in response.json()["applied"]["formulas"]}
    assert codes == {"F-100", "F-001"}
    assert set(_formula_map(client)) == {"F-001", "F-100"}
