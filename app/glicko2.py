"""
    glicko2
    ~~~~~~~

    The Glicko2 rating system.

    Vendored from https://github.com/sublee/glicko2 (3d56e29) with type
    annotations added and `quality_1vs1` dropped.

    :copyright: (c) 2012 by Heungsub Lee
    :license: BSD, see below.

Copyright (c) 2012-2016, Heungsub Lee
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

  Redistributions of source code must retain the above copyright notice, this
  list of conditions and the following disclaimer.

  Redistributions in binary form must reproduce the above copyright notice, this
  list of conditions and the following disclaimer in the documentation and/or
  other materials provided with the distribution.

  Neither the name of the {organization} nor the names of its
  contributors may be used to endorse or promote products derived from
  this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

import math
from collections.abc import Sequence

#: The actual score for win
WIN = 1.0
#: The actual score for draw
DRAW = 0.5
#: The actual score for loss
LOSS = 0.0


MU = 1500.0
PHI = 350.0
SIGMA = 0.06
TAU = 1.0
EPSILON = 0.000001


class Rating:
    def __init__(self, mu: float = MU, phi: float = PHI, sigma: float = SIGMA):
        self.mu = mu
        self.phi = phi
        self.sigma = sigma

    def __repr__(self) -> str:
        c = type(self)
        args = (c.__module__, c.__name__, self.mu, self.phi, self.sigma)
        return "%s.%s(mu=%.3f, phi=%.3f, sigma=%.3f)" % args


class Glicko2:
    def __init__(
        self,
        mu: float = MU,
        phi: float = PHI,
        sigma: float = SIGMA,
        tau: float = TAU,
        epsilon: float = EPSILON,
    ):
        self.mu = mu
        self.phi = phi
        self.sigma = sigma
        self.tau = tau
        self.epsilon = epsilon

    def create_rating(
        self,
        mu: float | None = None,
        phi: float | None = None,
        sigma: float | None = None,
    ) -> Rating:
        if mu is None:
            mu = self.mu
        if phi is None:
            phi = self.phi
        if sigma is None:
            sigma = self.sigma
        return Rating(mu, phi, sigma)

    def scale_down(self, rating: Rating, ratio: float = 173.7178) -> Rating:
        mu = (rating.mu - self.mu) / ratio
        phi = rating.phi / ratio
        return self.create_rating(mu, phi, rating.sigma)

    def scale_up(self, rating: Rating, ratio: float = 173.7178) -> Rating:
        mu = rating.mu * ratio + self.mu
        phi = rating.phi * ratio
        return self.create_rating(mu, phi, rating.sigma)

    def reduce_impact(self, rating: Rating) -> float:
        """The original form is `g(RD)`. This function reduces the impact of
        games as a function of an opponent's RD.
        """
        return 1.0 / math.sqrt(1 + (3 * rating.phi**2) / (math.pi**2))

    def expect_score(
        self, rating: Rating, other_rating: Rating, impact: float
    ) -> float:
        return 1.0 / (1 + math.exp(-impact * (rating.mu - other_rating.mu)))

    def determine_sigma(
        self, rating: Rating, difference: float, variance: float
    ) -> float:
        """Determines new sigma."""
        phi = rating.phi
        difference_squared = difference**2
        # 1. Let a = ln(s^2), and define f(x)
        alpha = math.log(rating.sigma**2)

        def f(x: float) -> float:
            """This function is twice the conditional log-posterior density of
            phi, and is the optimality criterion.
            """
            tmp = phi**2 + variance + math.exp(x)
            a = math.exp(x) * (difference_squared - tmp) / (2 * tmp**2)
            b = (x - alpha) / (self.tau**2)
            return a - b

        # 2. Set the initial values of the iterative algorithm.
        a = alpha
        if difference_squared > phi**2 + variance:
            b = math.log(difference_squared - phi**2 - variance)
        else:
            k = 1
            while f(alpha - k * math.sqrt(self.tau**2)) < 0:
                k += 1
            b = alpha - k * math.sqrt(self.tau**2)
        # 3. Let fA = f(A) and f(B) = f(B)
        f_a, f_b = f(a), f(b)
        # 4. While |B-A| > e, carry out the following steps.
        # (a) Let C = A + (A - B)fA / (fB-fA), and let fC = f(C).
        # (b) If fCfB < 0, then set A <- B and fA <- fB; otherwise, just set
        #     fA <- fA/2.
        # (c) Set B <- C and fB <- fC.
        # (d) Stop if |B-A| <= e. Repeat the above three steps otherwise.
        while abs(b - a) > self.epsilon:
            c = a + (a - b) * f_a / (f_b - f_a)
            f_c = f(c)
            if f_c * f_b < 0:
                a, f_a = b, f_b
            else:
                f_a /= 2
            b, f_b = c, f_c
        # 5. Once |B-A| <= e, set s' <- e^(A/2)
        return float(math.exp(1) ** (a / 2))

    def rate(self, rating: Rating, series: Sequence[tuple[float, Rating]]) -> Rating:
        # Step 2. For each player, convert the rating and RD's onto the
        #         Glicko-2 scale.
        rating = self.scale_down(rating)
        # Step 3. Compute the quantity v. This is the estimated variance of the
        #         team's/player's rating based only on game outcomes.
        # Step 4. Compute the quantity difference, the estimated improvement in
        #         rating by comparing the pre-period rating to the performance
        #         rating based only on game outcomes.
        variance_inv = 0.0
        difference = 0.0
        if not series:
            # If the team didn't play in the series, do only Step 6
            phi_star = math.sqrt(rating.phi**2 + rating.sigma**2)
            return self.scale_up(self.create_rating(rating.mu, phi_star, rating.sigma))
        for actual_score, other_rating in series:
            other_rating = self.scale_down(other_rating)
            impact = self.reduce_impact(other_rating)
            expected_score = self.expect_score(rating, other_rating, impact)
            variance_inv += impact**2 * expected_score * (1 - expected_score)
            difference += impact * (actual_score - expected_score)
        difference /= variance_inv
        variance = 1.0 / variance_inv
        # Step 5. Determine the new value, Sigma', ot the sigma. This
        #         computation requires iteration.
        sigma = self.determine_sigma(rating, difference, variance)
        # Step 6. Update the rating deviation to the new pre-rating period
        #         value, Phi*.
        phi_star = math.sqrt(rating.phi**2 + sigma**2)
        # Step 7. Update the rating and RD to the new values, Mu' and Phi'.
        phi = 1.0 / math.sqrt(1 / phi_star**2 + 1 / variance)
        mu = rating.mu + phi**2 * (difference / variance)
        # Step 8. Convert ratings and RD's back to original scale.
        return self.scale_up(self.create_rating(mu, phi, sigma))

    def rate_1vs1(
        self, rating1: Rating, rating2: Rating, drawn: bool = False
    ) -> tuple[Rating, Rating]:
        return (
            self.rate(rating1, [(DRAW if drawn else WIN, rating2)]),
            self.rate(rating2, [(DRAW if drawn else LOSS, rating1)]),
        )
