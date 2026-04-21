from backend.parser import extract_from_payload
from tests.fixtures import home_timeline_payload, make_tweet, make_user


def test_extract_basic_timeline():
    alice = make_user("100", "alice", "Alice", followers=500)
    bob = make_user("200", "bob", "Bob")
    t1 = make_tweet("1001", alice, "hello", likes=10, views=200)
    t2 = make_tweet("1002", bob, "reply", reply_to_tweet_id="1001", reply_to_user_id="100", conversation_id="1001")
    out = extract_from_payload(home_timeline_payload([t1, t2]))
    assert {a["user_id"] for a in out["authors"]} == {"100", "200"}
    tweets_by_id = {t["tweet_id"]: t for t in out["tweets"]}
    assert tweets_by_id["1001"]["text"] == "hello"
    assert tweets_by_id["1001"]["author_id"] == "100"
    assert tweets_by_id["1001"]["conversation_id"] == "1001"
    assert tweets_by_id["1002"]["reply_to_tweet_id"] == "1001"
    eng = {e["tweet_id"]: e for e in out["engagements"]}
    assert eng["1001"]["likes"] == 10
    assert eng["1001"]["views"] == 200


def test_missing_fields_tolerated():
    payload = {"data": {"weird_shape": {"__typename": "Tweet", "rest_id": "9"}}}
    out = extract_from_payload(payload)
    assert len(out["tweets"]) == 1
    # Author missing is fine
    assert out["tweets"][0]["tweet_id"] == "9"


def test_empty_payload():
    out = extract_from_payload({})
    assert out == {"authors": [], "tweets": [], "engagements": []}
