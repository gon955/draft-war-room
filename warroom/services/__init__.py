"""Work that is neither an HTTP concern nor pure algorithm.

Routes stay thin and the valuation package stays framework-free; anything that
has to touch both the DB and the domain lands here.
"""
