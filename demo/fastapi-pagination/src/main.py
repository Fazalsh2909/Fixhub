"""Demo pagination service — BUG: off-by-one, page 1 skips the first item.

Correct: page=1,size=2 over [a,b,c,d] -> [a,b]. Buggy: -> [b,c].
Fixhub's job: reproduce via pytest, fix the slice, verify PASS.
"""
from fastapi import FastAPI

app = FastAPI()

ITEMS = ["a", "b", "c", "d", "e"]


def paginate(items: list[str], page: int, size: int) -> list[str]:
    # BUG: page*size instead of (page-1)*size — classic off-by-one
    start = page * size
    return items[start : start + size]


@app.get("/items")
def list_items(page: int = 1, size: int = 2):
    return {"items": paginate(ITEMS, page, size), "page": page, "size": size}
