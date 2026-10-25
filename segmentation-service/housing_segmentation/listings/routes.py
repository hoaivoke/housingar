from typing import Annotated

from fastapi import APIRouter, Query, Request

from .rooms import RoomLayout
from .service import ListingQuery, RoomFurniture

router = APIRouter(prefix="/v1/listings", tags=["listings"])


@router.get("")
def search(request: Request, query: Annotated[ListingQuery, Query()]):
    return request.app.state.listings.search(query)


@router.get("/{listing_id}")
def detail(request: Request, listing_id: str):
    return request.app.state.listings.detail(listing_id)


@router.get("/{listing_id}/layout")
def layout(request: Request, listing_id: str):
    return request.app.state.listings.layout(listing_id)


@router.put("/{listing_id}/layout")
def save_layout(request: Request, listing_id: str, payload: RoomLayout):
    return request.app.state.listings.save_layout(listing_id, payload)


@router.delete("/{listing_id}/layout")
def reset_layout(request: Request, listing_id: str):
    return request.app.state.listings.reset_layout(listing_id)


@router.get("/{listing_id}/furniture")
def furniture(request: Request, listing_id: str):
    return request.app.state.listings.furniture(listing_id)


@router.put("/{listing_id}/furniture")
def save_furniture(request: Request, listing_id: str, payload: RoomFurniture):
    return request.app.state.listings.save_furniture(listing_id, payload)
