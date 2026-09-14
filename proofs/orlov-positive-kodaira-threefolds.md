# Derived Equivalence and Chow Motives of Threefolds of Positive Kodaira Dimension

**Author**: Franta 

14 September 2026

## Abstract

We prove that smooth projective complex threefolds with equivalent bounded derived categories of coherent sheaves have isomorphic rational Chow motives whenever their Kodaira dimension is positive. The argument does not assume that the canonical bundle on either threefold is semiample. Its motivic ingredient is a cancellation theorem for correspondences supported on a product of proper closed subsets: in dimension three, these correspondences factor through Lefschetz motives and twists of degree-one curve motives. The minimal model program identifies the complements of the stable canonical base loci with complements of subsets of dimension at most one on good minimal models. Properness of the original Fourier–Mukai support then gives an alternative. Either an omitted curve dominates the Iitaka base and the original threefolds are birational, or canonical-section localization induces an equivalence of the proper generic fibres over the function field of the common base. The known comparison of motives in dimensions at most two, together with degreewise Hodge invariance and boundary cancellation, completes the proof.

**Keywords:** Chow motives; derived equivalence; threefolds; Kodaira dimension; Iitaka fibration; Fourier–Mukai transforms.

## 1. Introduction

Orlov's conjecture predicts that derived-equivalent smooth projective varieties have isomorphic Chow motives with rational coefficients [Orl05]. A Fourier–Mukai equivalence provides mutually inverse correspondences after allowing components of different codimensions and Tate twists. Recovering an isomorphism of ordinary Chow motives requires controlling the contributions that change degree. For surfaces, this can be done using the structure of their Chow–Künneth decompositions; see [FV21, Theorem 1.1].

The purpose of this paper is to establish the following three-dimensional case.

**Theorem 1.1.** Let $X$ and $Y$ be smooth connected projective varieties of dimension three over $\mathbb C$. Suppose that there is an exact $\mathbb C$-linear equivalence

$$
\Phi:D^b(X)\xrightarrow{\sim}D^b(Y).
$$

If $\kappa(X)>0$, then

$$
h(X)_{\mathbb Q}\simeq h(Y)_{\mathbb Q}.
$$

Here the derived categories are untwisted, and the conclusion is an isomorphism in the category of rational Chow motives. Neither compatibility with the multiplicative structure nor an integral isomorphism is asserted. There is no hypothesis on the irregularity of the threefolds.

The central observation concerns three-dimensional cycles supported on a product of divisors. After resolving the divisors, such a cycle lifts to a divisor class on a product of smooth surfaces. The corresponding morphism of motives therefore factors through a sum of Lefschetz motives and twists of degree-one curve motives. These motives admit cancellation against arbitrary Chow motives. Consequently, an isomorphism modulo the associated factorization ideal lifts to an actual isomorphism whenever the graded rational Hodge realizations agree.

The geometric part of the proof uses good minimal models. Passing to a minimal model can remove points from a generic surface fibre, so the derived category obtained by inverting canonical sections need not be the category of a proper generic fibre. We handle this issue using the support of the original projective Fourier–Mukai kernel. If the omitted locus meets the generic surface fibre, properness forces the images of general point objects to have zero-dimensional support, and Toda's birationality criterion applies. Otherwise, the proper generic fibres occur inside the original threefolds and can be compared by canonical-section localization.

Sections 2 and 3 establish the motivic cancellation and spreading arguments. Section 4 proves the geometric alternative. Section 5 identifies the generic derived categories and compares their motives. The proof of Theorem 1.1 is given in Section 6.

### Conventions

All Chow groups have rational coefficients. We write $\mathrm{CH}_i(Z)$ for cycles of dimension $i$ modulo rational equivalence, and $\mathrm{CH}^i(Z)$ for cycles of codimension $i$ when $Z$ is smooth and equidimensional. We use contravariant Chow motives, with

$$
\operatorname{Hom}\bigl(h(U)(a),h(V)(b)\bigr)
=\mathrm{CH}^{\dim U+b-a}(U\times V),
\qquad
\mathbb L=\mathbb 1(-1).
$$

Composition is read from right to left. The category of Chow motives is understood to be additive and idempotent complete. Over $\mathbb C$, $H(M)$ denotes the graded rational Betti realization of a motive $M$, including its Hodge structures and Tate twists. For a smooth variety $V$, we identify $D^b(V)=D^b\!\operatorname{Coh}(V)$ with $\operatorname{Perf}(V)$. The notation $\operatorname{Supp}(E)$ for a bounded coherent complex means the union of the supports of its cohomology sheaves. All supports and base loci used in set-theoretic arguments are given their reduced structures.

## 2. Boundary correspondences and cancellation

Let $\mathcal B_3$ be the full additive idempotent-complete subcategory of rational Chow motives over $\mathbb C$ generated by

$$
\mathbb L,\qquad h^1(C)(-1),\qquad \mathbb L^2,
\tag{2.1}
$$

where $C$ ranges over smooth connected projective complex curves. Thus objects of $\mathcal B_3$ are direct summands of finite direct sums of these generators. For motives $M,N$, let

$$
\mathcal I(M,N)=
\{f:M\longrightarrow N\mid f\text{ factors through an object of }\mathcal B_3\}.
\tag{2.2}
$$

These subgroups form a two-sided ideal. Closure under addition follows by taking the direct sum of the intermediate objects. An isomorphism modulo $\mathcal I$ means an isomorphism in the additive quotient by this ideal.

### 2.1. Correspondences with proper support on both sides

**Lemma 2.1.** Let $X$ and $Y$ be smooth projective complex threefolds. If $z\in\mathrm{CH}^3(X\times Y)$ has a cycle representative supported on $D\times E$, where $D\subsetneq X$ and $E\subsetneq Y$ are closed, then

$$
z\in\mathcal I(h(X),h(Y)).
$$

*Proof.* Enlarge $D$ and $E$ to divisors, and resolve their reduced irreducible components. We obtain smooth projective surfaces $S_i,T_j$ and proper maps

$$
\coprod_{i,j}S_i\times T_j\longrightarrow D\times E
$$

that are jointly surjective. Pushforward on rational Chow groups of dimension three is surjective. Indeed, for an integral three-dimensional subvariety $W\subset D\times E$, choose a closed point of a nonempty fibre over its generic point. The closure of this point is a three-dimensional subvariety $W'$ upstairs, and

$$
[W']\longmapsto d[W]
$$

for a positive finite degree $d$. Dividing by $d$ gives a lift. This argument also applies when $W$ lies in the singular locus of $D\times E$.

It is therefore enough to consider the pushforward of a class

$$
\delta\in\mathrm{CH}^1(S\times T)
$$

under maps $i:S\to X$ and $j:T\to Y$. The resulting morphism factors as

$$
h(X)\xrightarrow{i^*}h(S)
\xrightarrow{\delta}h(T)(-1)
\xrightarrow{j_*}h(Y).
\tag{2.3}
$$

Choose the surface Chow–Künneth decompositions of [FV21, Theorem 1.4]. The component of $\delta$ from $h^a(S)$ to $h^b(T)(-1)$ vanishes unless

$$
b+2\leq a\leq b+3;
$$

this is [FV21, Theorem 1.4(ii), equation (3)] with source dimension two and twist $-1$. Thus the only possible pairs $(a,b)$ are

$$
(2,0),\quad(3,0),\quad(3,1),\quad(4,1),\quad(4,2).
$$

When $b=0$, the component factors through $h^0(T)(-1)=\mathbb L$. When $a=4$, it factors through $h^4(S)=\mathbb L^2$. The remaining component factors through

$$
h^3(S)\simeq h^1(S)(-1),
$$

which is a direct summand of $h^1(C)(-1)$ for a suitable smooth projective curve $C$. Each term of (2.3) therefore factors through $\mathcal B_3$. Their sum has the same property. $\square$

### 2.2. The structure of the boundary category

**Lemma 2.2.** The realization functor on $\mathcal B_3$ is full. Its kernel is a square-zero ideal. For each $P\in\mathcal B_3$, the ring $\operatorname{End}(P)$ has a square-zero ideal with semisimple Artinian quotient. Moreover,

$$
H(P)\simeq H(Q),\quad P,Q\in\mathcal B_3
\quad\Longrightarrow\quad P\simeq Q.
$$

*Proof.* First consider finite direct sums of the generators (2.1), ordered by their realization degrees $2,3,4$. Applying the pointed-curve Chow projectors to divisor and fundamental classes shows that the only potentially nonzero off-diagonal morphisms are

$$
\mathbb L^2\longrightarrow h^1(C)(-1),
\qquad
h^1(C)(-1)\longrightarrow\mathbb L.
\tag{2.4}
$$

These morphisms come from degree-zero divisor classes. Morphisms between $\mathbb L$ and $\mathbb L^2$ vanish in both directions. Hence any composite of two off-diagonal morphisms is zero.

The diagonal blocks in degrees two and four are rational matrices. The degree-three block is the category of degree-one curve motives, with a common Tate twist. Its morphisms are identified with homomorphisms of abelian varieties up to isogeny. Rational Hodge morphisms between their degree-one realizations are precisely such homomorphisms: after multiplying by an integer, a Hodge morphism preserves the integral lattices and determines a homomorphism of complex tori, which is algebraic for abelian varieties. Thus realization is full and faithful on each diagonal block. Poincaré complete reducibility makes the corresponding endomorphism algebras semisimple and finite-dimensional over $\mathbb Q$.

It follows that the realization kernel on sums of generators is exactly the off-diagonal ideal in (2.4), and its square is zero. The quotient endomorphism ring is semisimple Artinian. For direct summands, extend a Hodge morphism to the ambient sums, lift it there, and compose with the defining Chow projectors. This proves fullness on all of $\mathcal B_3$. The square-zero assertion passes to these summands, as does semisimplicity of the quotient, since a corner of a semisimple Artinian ring is semisimple Artinian.

Finally, lift a Hodge isomorphism $H(P)\to H(Q)$ and its inverse to morphisms $u:P\to Q$ and $v:Q\to P$. Both $vu$ and $uv$ differ from the identity by elements of the square-zero kernel, and hence are invertible. Thus $u$ has both a left inverse and a right inverse, which coincide. Therefore $u$ is an isomorphism. $\square$

**Lemma 2.3 (cancellation).** Let $M,N$ be rational Chow motives and let $P\in\mathcal B_3$. Then

$$
M\oplus P\simeq N\oplus P
\quad\Longrightarrow\quad M\simeq N.
$$

*Proof.* Put $R=\operatorname{End}(P)$. We first establish the following stable-range property:

$$
dR+aR=R\quad\Longrightarrow\quad
d+ak\in R^\times\text{ for some }k\in R.
\tag{2.5}
$$

By Lemma 2.2, $R$ has a square-zero ideal $J$ and $R/J$ is semisimple Artinian. In a simple matrix factor of $R/J$, view $d$ and $a$ as endomorphisms of a finite-dimensional vector space over a division ring. The hypothesis says that $\operatorname{im}(d)+\operatorname{im}(a)$ is the whole space. Choose a complement to $\ker(d)$. The restriction of $d$ to this complement maps isomorphically onto $\operatorname{im}(d)$. Choose $k$ on $\ker(d)$ so that the induced map $ak$ identifies $\ker(d)$ with the quotient by $\operatorname{im}(d)$, and set $k=0$ on the chosen complement. Then $d+ak$ is invertible. Make this choice in every simple factor and lift $k$ to $R$. Units lift across nilpotent ideals, proving (2.5).

Write an isomorphism and its inverse in block form as

$$
F=\begin{pmatrix}a&b\\c&d\end{pmatrix}:
M\oplus P\longrightarrow N\oplus P,
\qquad
F^{-1}=\begin{pmatrix}e&f\\g&h\end{pmatrix}.
$$

The lower-right block of $FF^{-1}=1$ gives $cf+dh=1_P$. By (2.5), there is $k\in R$ such that $d+cfk$ is invertible. Precomposing $F$ with the automorphism

$$
\begin{pmatrix}1_M&fk\\0&1_P\end{pmatrix}
$$

makes its lower-right block invertible. Elementary block elimination then transforms it into a block-diagonal isomorphism, whose upper-left block is an isomorphism $M\to N$. $\square$

### 2.3. Lifting an isomorphism modulo the boundary ideal

**Proposition 2.4.** Let $M,N$ be rational Chow motives over $\mathbb C$. Suppose that there are morphisms $f:M\to N$ and $g:N\to M$ with

$$
gf-1_M\in\mathcal I(M,M),
\qquad
fg-1_N\in\mathcal I(N,N).
\tag{2.6}
$$

If $H(M)\simeq H(N)$ as graded rational Hodge structures, then $M\simeq N$.

*Proof.* Factor $gf-1_M=sr$ through some $P\in\mathcal B_3$, where $r:M\to P$ and $s:P\to M$. Define

$$
i=\binom{f}{r}:M\longrightarrow N\oplus P,
\qquad
j=(g,-s):N\oplus P\longrightarrow M.
$$

Then $ji=1_M$. Let $K$ be the complementary summand cut out by $1-ij$. Thus

$$
M\oplus K\simeq N\oplus P.
\tag{2.7}
$$

Every block of $1-ij$ factors through an object of $\mathcal B_3$: its upper-left block is $1-fg$, and every other block has source or target $P$. Consequently $1_K$ factors through an object of $\mathcal B_3$, so $K$ is a retract of such an object and itself belongs to $\mathcal B_3$.

The category of polarizable pure rational Hodge structures is semisimple. Apply this in each degree to the realization of (2.7), and use $H(M)\simeq H(N)$, to obtain $H(K)\simeq H(P)$. Lemma 2.2 gives $K\simeq P$. Substituting in (2.7) and applying Lemma 2.3 proves the result. $\square$

No finite-dimensionality or nilpotence assumption on $M$ or $N$ is used in this proposition. The square-zero calculation is confined to the explicitly described category $\mathcal B_3$.

## 3. Spreading generic correspondences and changing smooth models

### 3.1. Generic fibres over a common base

**Proposition 3.1.** Let $X$ and $Y$ be smooth connected projective complex threefolds with dominant morphisms

$$
f:X\longrightarrow B,
\qquad
g:Y\longrightarrow B
$$

to an integral projective variety of positive dimension. Write $K=\mathbb C(B)$. Suppose that the generic fibres are smooth projective and that

$$
h(X_\eta)\simeq h(Y_\eta)
\quad\text{over }K.
$$

Then $h(X)$ and $h(Y)$ are isomorphic modulo $\mathcal I$. If, in addition, their rational Hodge structures are isomorphic degree by degree, then $h(X)\simeq h(Y)$.

*Proof.* Put $b=\dim B$ and $r=3-b$. Choose inverse correspondences defined over $K$,

$$
\alpha_\eta\in\mathrm{CH}^r(X_\eta\times_K Y_\eta),
\qquad
\beta_\eta\in\mathrm{CH}^r(Y_\eta\times_K X_\eta).
$$

Closing representatives gives classes

$$
\alpha\in\mathrm{CH}_3(X\times_B Y),
\qquad
\beta\in\mathrm{CH}_3(Y\times_B X).
\tag{3.1}
$$

For smooth total spaces projective over $B$, relative correspondences have an associative composition that preserves support over $B$ [CH07, §3.1]. Explicitly, take the external product, apply refined Gysin pullback along the diagonal of the smooth absolute middle variety, and then use proper pushforward. The dimension shift is the dimension of that middle variety, so in the present case

$$
\mathrm{CH}_3(X\times_B Y)\otimes
\mathrm{CH}_3(Y\times_B X)
\longrightarrow\mathrm{CH}_3(X\times_B X).
$$

Pushforward to the absolute products carries this operation to the usual composition of Chow correspondences. This construction is valid even if $B$ is singular or the morphisms have fibres of different dimensions.

We record the generic compatibility needed here. After replacing $B$ by a smooth nonempty open, the two families are smooth. In the construction of relative composition, equality of the two middle points first imposes equality of their base coordinates and then imposes the relative diagonal of the middle family. Refined base change and the composition rule for these regular embeddings identify restriction to the generic point with composition along the diagonal of the generic middle fibre. Thus the restrictions of

$$
\beta\circ_B\alpha-[\Delta_X],
\qquad
\alpha\circ_B\beta-[\Delta_Y]
\tag{3.2}
$$

are zero in the Chow groups of the respective generic fibre products.

A rational equivalence over $K$ is represented by finitely many subvarieties and rational functions. Spreading these data shows that both defects in (3.2) vanish after a common shrinking of $B$. By Chow localization [Ful98, Proposition 1.8], they therefore come from cycles supported over a proper closed subset $Z\subsetneq B$. For example, the first lies in the image of

$$
\mathrm{CH}_3\bigl((X\times_B X)_Z\bigr)
\longrightarrow\mathrm{CH}_3(X\times_B X).
$$

Both projections of this support lie in $f^{-1}(Z)\subsetneq X$. Lemma 2.1 therefore puts the absolute image of the first defect in $\mathcal I(h(X),h(X))$, and similarly for $Y$. Hence the absolute images of $\alpha$ and $\beta$ are inverse modulo $\mathcal I$. The final assertion is Proposition 2.4. $\square$

### 3.2. Birational modifications

**Lemma 3.2.** Birational smooth projective complex threefolds have isomorphic Chow motives modulo $\mathcal I$.

*Proof.* First let $p:Z\to X$ be a projective birational morphism between smooth projective threefolds. The graph correspondences satisfy

$$
p_*p^*=1_{h(X)}.
$$

The other composition has a supported representative on $Z\times_X Z$, obtained by refined intersection along $\Delta_X$. Let $U\subset X$ be an open set over which $p$ is an isomorphism and put $E=p^{-1}(X\setminus U)$. Set-theoretically,

$$
Z\times_X Z\subset\Delta_Z\cup(E\times E).
$$

A three-dimensional cycle on this union is a multiple of $\Delta_Z$ plus a cycle supported on $E\times E$. Restriction over $U$ shows that the coefficient of the diagonal in the composition is one. Thus $p^*p_*-1_{h(Z)}$ has proper support on both sides and belongs to $\mathcal I$ by Lemma 2.1.

For a general birational map, resolve its graph and apply the preceding argument to the two resulting projective birational morphisms. $\square$

**Corollary 3.3.** If two smooth projective complex threefolds are birational and have degreewise isomorphic rational Hodge structures, their rational Chow motives are isomorphic.

*Proof.* Combine Lemma 3.2 with Proposition 2.4. $\square$

## 4. Canonical free loci and Fourier–Mukai support

Throughout this section, $X,Y$ and $\Phi$ satisfy the hypotheses of Theorem 1.1. By representability [Orl97], choose Fourier–Mukai kernels $P\in D^b(X\times Y)$ and $Q\in D^b(Y\times X)$ for $\Phi$ and its inverse.

### 4.1. Compatibility with actual canonical-section maps

Serre-functor compatibility gives compatible kernel isomorphisms

$$
P\otimes p_X^*\omega_X^{\otimes m}
\simeq
P\otimes p_Y^*\omega_Y^{\otimes m}
\tag{4.1}
$$

and an isomorphism of graded canonical rings

$$
\rho:R(X,K_X)\xrightarrow{\sim}R(Y,K_Y),
\qquad
R(X,K_X)=\bigoplus_{m\geq0}H^0(X,\omega_X^{\otimes m}).
\tag{4.2}
$$

We use the compatibility at the level of section morphisms established in [Tod06, Proposition 4.1, Lemmas 4.2–4.3, and Corollary 4.4]. Namely, for a section $s\in H^0(X,\omega_X^{\otimes m})$, (4.1) identifies multiplication by $p_X^*s$ on $P$ with multiplication by $p_Y^*\rho(s)$. Consequently, for every $E\in D^b(X)$, the morphism

$$
s_E:E\longrightarrow E\otimes\omega_X^{\otimes m}
\tag{4.3}
$$

is carried by $\Phi$ to the corresponding multiplication morphism on $\Phi(E)$, after the Serre identification.

For clarity, these statements concern actual section maps. One obtains them by conjugating diagonal-kernel morphisms with $P$ and $Q$. In degree zero,

$$
\operatorname{Hom}_{X\times X}
\bigl(\Delta_*\mathcal O_X,\Delta_*\omega_X^{\otimes m}\bigr)
=H^0(X,\omega_X^{\otimes m}).
$$

Convolution turns such a morphism into multiplication on $P$, and composition with tensor twists gives the multiplication in (4.2). Thus no identification with the space of all triangulated natural transformations is needed. In particular, (4.2) implies $\kappa(Y)=\kappa(X)>0$.

### 4.2. Free loci on good minimal models

**Lemma 4.1.** There are projective $\mathbb Q$-factorial terminal minimal models $T_X,T_Y$, closed subsets

$$
C_X\subset T_X,\qquad C_Y\subset T_Y,
\qquad \dim C_X,\dim C_Y\leq1,
$$

and a sufficiently divisible integer $m>0$ such that

$$
X^\circ:=X\setminus\operatorname{Bs}|mK_X|
\simeq T_X\setminus C_X,
\qquad
Y^\circ:=Y\setminus\operatorname{Bs}|mK_Y|
\simeq T_Y\setminus C_Y.
\tag{4.4}
$$

The base loci in (4.4) are the stable canonical base loci. There are connected-fibre morphisms to a common normal integral projective variety,

$$
f:T_X\longrightarrow B,
\qquad
g:T_Y\longrightarrow B,
\qquad
\dim B=\kappa(X),
\tag{4.5}
$$

induced by the common canonical ring. For an ample line bundle $A$ on $B$, after increasing $m$ if necessary,

$$
\mathcal O_{T_X}(mK_{T_X})\simeq f^*A,
\qquad
\mathcal O_{T_Y}(mK_{T_Y})\simeq g^*A.
\tag{4.6}
$$

Their generic fibres are smooth projective geometrically integral varieties over $K=\mathbb C(B)$ of dimension $3-\dim B$.

*Proof.* Run a $K$-negative threefold minimal model program on each variety. The existence and termination results in dimension three give projective $\mathbb Q$-factorial terminal minimal models, and abundance makes their canonical divisors semiample; see [FA92] and [Kaw92]. The Mori-fibre-space outcome is excluded by positive Kodaira dimension. Canonical rings are preserved by the program.

There are only finitely many intermediate models. Choose $m$ divisible enough that $mK$ is Cartier on all of them, that the systems at the minimal endpoints are basepoint-free, and that every $\operatorname{Bs}|mK|$ is the corresponding stable base locus. Such a common multiple exists: the base loci along factorial multiples form a descending chain of closed subsets, which stabilizes. Increasing a stabilized factorial multiple preserves its base locus. We may also require the relevant Veronese ring to give an embedding of the canonical base by an ample line bundle.

For an intermediate model $V$, put $F_V=V\setminus\operatorname{Bs}|mK_V|$. Consider first a $K$-negative divisorial contraction $c:V\to V'$. Each point of its exceptional locus lies on a contracted projective curve $\Gamma$ with $\deg(\mathcal O_V(mK_V)|_\Gamma)<0$. Every global section vanishes on $\Gamma$, so

$$
F_V\cap\operatorname{Exc}(c)=\varnothing.
$$

On the isomorphism locus of the contraction, the plurisections agree. Hence

$$
F_V\simeq F_{V'}\setminus c(\operatorname{Exc}(c)),
\tag{4.7}
$$

where the centre $c(\operatorname{Exc}(c))$ has dimension at most one.

For a flip $V\to Z\leftarrow V^+$, the same negativity argument places the flipping locus in the base locus. The models and their plurisections agree on the complements of the flipping and flipped loci, giving

$$
F_V\simeq F_{V^+}\setminus\operatorname{Exc}(V^+\to Z).
\tag{4.8}
$$

The flipped locus has dimension at most one.

We now iterate (4.7) and (4.8). At each step, transfer the previously omitted subset through the common isomorphism locus, take its closure, and add the new contraction centre or flipped locus. The transferred part has dimension at most one, and taking its closure adds points only on the newly omitted locus. The complement inside the new free locus is exactly the image of the retained open set. Thus the omitted sets remain closed of dimension at most one. At the semiample endpoint the free locus is the whole minimal model, proving (4.4). This also shows that $C_X$ and $C_Y$ contain the respective singular loci, since their complements are isomorphic to smooth open subsets of $X$ and $Y$.

By (4.2) and preservation of canonical rings, the endpoints have the same canonical base. The Proj construction for the full section ring, or equivalently Stein factorization of the semiample morphism, gives a normal base and connected fibres. Taking a sufficiently ample Veronese gives (4.6).

Terminal threefold singularities are isolated [Kol90, Proposition 2.7]. Since $\dim B>0$, their images do not contain the generic point of $B$. Generic smoothness, followed by properness to remove the image of the nonsmooth locus, therefore gives smooth families over a nonempty open of $B$. Connected fibres and normality of $B$ give $f_*\mathcal O_{T_X}=\mathcal O_B$ and similarly for $g$. Consequently the smooth generic fibres are geometrically connected and hence geometrically integral. Their dimension is $3-\dim B$. $\square$

### 4.3. Properness on the free loci

**Lemma 4.2.** In the notation of Lemma 4.1, put

$$
W^\circ=\operatorname{Supp}(P)\cap(X^\circ\times Y^\circ).
$$

Then $W^\circ$ is proper over both $X^\circ$ and $Y^\circ$, and

$$
W^\circ\subset X^\circ\times_B Y^\circ.
\tag{4.9}
$$

Let $W$ be its closure in $T_X\times_B T_Y$ under the identifications (4.4). Then

$$
\begin{aligned}
W\cap\bigl((T_X\setminus C_X)\times C_Y\bigr)&=\varnothing,\\
W\cap\bigl(C_X\times(T_Y\setminus C_Y)\bigr)&=\varnothing.
\end{aligned}
\tag{4.10}
$$

*Proof.* Suppose that $x\in X^\circ$ and $y\notin Y^\circ$. Choose $s\in H^0(X,mK_X)$ with $s(x)\neq0$. Then $\rho(s)(y)=0$. Apply the section compatibility of §4.1 to the derived residue fibre of $P$ at $(x,y)$. On this fibre, multiplication by $s$ is invertible and multiplication by $\rho(s)$ is zero. The fibre must therefore vanish. Derived Nakayama implies $(x,y)\notin\operatorname{Supp}(P)$. Interchanging the factors gives

$$
\operatorname{Supp}(P)\subset
(X^\circ\times Y^\circ)
\cup
\bigl((X\setminus X^\circ)\times(Y\setminus Y^\circ)\bigr).
\tag{4.11}
$$

The support of the original kernel is projective over both factors. By (4.11), its full restriction over $X^\circ$ is precisely $W^\circ$, which is consequently proper over $X^\circ$. The same holds over $Y^\circ$.

Choose sections $s_0,\ldots,s_N$ whose counterparts on the minimal models define the maps to $B$ followed by a projective embedding. On a chart where $s_0$ and $\rho(s_0)$ are nonzero, divide the identities for multiplication by $s_i$ by the identity for $s_0$. Each function

$$
p_X^*(s_i/s_0)-p_Y^*(\rho(s_i)/\rho(s_0))
$$

acts by zero on the restricted kernel and thus vanishes on its support. These equalities of projective coordinates prove (4.9).

It remains to verify (4.10). The map from $W^\circ$ to $T_X\times Y^\circ$ is proper: $W^\circ\to Y^\circ$ is proper and the target is separated over $Y^\circ$. Its image is therefore closed. Taking its closure in $T_X\times T_Y$ adds no point whose second coordinate remains in $Y^\circ$. Such a point could not have its first coordinate in $C_X$. The symmetric argument excludes points whose first coordinate is in $X^\circ$ and whose second coordinate is in $C_Y$. This proves (4.10). $\square$

### 4.4. An omitted horizontal curve forces birationality

**Proposition 4.3.** Suppose that $\dim B=1$. If either $C_X$ or $C_Y$ dominates $B$, then $X$ and $Y$ are birational.

*Proof.* Suppose first that $C_X$ dominates $B$. Write

$$
S=(T_X)_\eta,\qquad T=(T_Y)_\eta,
\qquad A_X=(C_X)_\eta,\qquad A_Y=(C_Y)_\eta.
$$

Then $S,T$ are smooth projective geometrically integral surfaces over $K=\mathbb C(B)$. The scheme $A_X$ is finite and nonempty, and $A_Y$ is finite.

Let $W_0$ be an irreducible component of $W^\circ$ dominating $X^\circ$, and let $Z$ be its closure in $T_X\times_B T_Y$. The map $Z\to T_X$ is proper and dominant, hence surjective. Its generic fibre $Z_\eta$ is integral and maps properly and surjectively to $S$.

Choose a closed point $a\in A_X$. By (4.10), the fibre of $Z_\eta\to S$ over $a$ is contained in $(A_Y)_{K(a)}$. It is nonempty by surjectivity and has dimension zero because $A_Y$ is finite. The fibre-dimension inequality gives

$$
\dim Z_\eta-\dim S
\leq\dim (Z_\eta)_a=0.
$$

Since $Z_\eta$ dominates the surface $S$, it follows that $\dim Z_\eta=2$ and $\dim Z=3$. Thus every component of the kernel support dominating $X$ has dimension three.

There are only finitely many support components. The projections of all nondominating components are proper closed subsets of $X$, because the original support is projective. A general point $x\in X^\circ$ avoids these images and the loci where the fibres of the dominating components have positive dimension. Consequently,

$$
\dim\operatorname{Supp}\Phi(\mathcal O_x)=0.
$$

The object is nonzero since $\Phi$ is an equivalence. Toda's point-support criterion [Tod06, Lemma 7.3] now implies that $X$ and $Y$ are birational. If $C_Y$ dominates $B$, apply the same argument to the inverse kernel $Q$. $\square$

If $\dim B\geq2$, neither omitted subset can dominate $B$, by its dimension bound. If $\dim B=1$ and Proposition 4.3 does not apply, both are again nondominant. Thus, outside the birational alternative, there is a nonempty regular affine open

$$
U\subset B\setminus\bigl(f(C_X)\cup g(C_Y)\bigr)
\tag{4.12}
$$

over which both families are smooth. Their restrictions $(T_X)_U$ and $(T_Y)_U$ are proper over $U$ and, by (4.4), are actual open subvarieties of the original $X$ and $Y$.

## 5. The proper generic fibres

Assume in this section that neither $C_X$ nor $C_Y$ dominates $B$, and choose $U$ as in (4.12). Write

$$
G_X=(T_X)_\eta,\qquad G_Y=(T_Y)_\eta,
\qquad K=\mathbb C(B).
$$

These proper generic fibres map to $X$ and $Y$ through the open families just constructed.

### 5.1. Canonical-section localization

**Proposition 5.1.** There is an exact $K$-linear equivalence

$$
D^b(G_X)\simeq D^b(G_Y).
\tag{5.1}
$$

*Proof.* Let $\mathcal T_X$ be the thick subcategory of $\operatorname{Perf}(X)$ generated by the cones of all multiplication maps

$$
s_E:E\longrightarrow E\otimes\omega_X^{\otimes mr},
\qquad
0\neq s\in H^0(X,mrK_X),\quad r>0,
\tag{5.2}
$$

as $E$ ranges over all perfect complexes. Define $\mathcal T_Y$ analogously. We first prove that

$$
\mathcal T_X=
\ker\bigl(\operatorname{Perf}(X)\longrightarrow\operatorname{Perf}(G_X)\bigr).
\tag{5.3}
$$

Every nonzero section in (5.2) is invertible on $G_X$: there it is the pullback of a nonzero section of $A^r$ at the generic point of $B$. Hence each generating cone restricts to zero.

Conversely, let $F$ be a coherent sheaf on $X$ whose restriction to $G_X$ is zero. Consider the finitely many irreducible components of $\operatorname{Supp}(F)$ that meet $X^\circ$. Their images under $X^\circ\to B$ are nondense, since a component dominating $B$ would meet the generic fibre. Let $Z\subsetneq B$ be the union of the closures of these images. For $r$ sufficiently large, ampleness supplies a nonzero section of $A^r$ vanishing on $Z$.

Let $s\in H^0(X,mrK_X)$ be the corresponding canonical section. It vanishes on the portions of $\operatorname{Supp}(F)$ inside $X^\circ$ and hence on their closures. It also vanishes on every component contained in $X\setminus X^\circ$, because this is the stable canonical base locus, realized by every positive multiple of the chosen $m$. Thus $s$ vanishes on all of $\operatorname{Supp}(F)$.

By noetherianity, some power $s^N$ annihilates $F$. This can be checked on a finite affine cover trivializing the line bundle and then made uniform by increasing $N$. The map

$$
s_F^N:F\longrightarrow F\otimes\omega_X^{\otimes mrN}
$$

is zero. Its cone lies in $\mathcal T_X$ and contains $F[1]$ as a direct summand. Therefore $F\in\mathcal T_X$. Since $X$ is smooth, coherent sheaves are perfect; finite truncation triangles establish the same assertion for bounded complexes whose restriction is zero. This proves (5.3). The argument for $Y$ is identical.

The compatibility of the actual section maps in §4.1 gives

$$
\Phi(\mathcal T_X)=\mathcal T_Y.
\tag{5.4}
$$

We next identify the quotients. Write $U=\operatorname{Spec}R$. For the quasi-compact open immersion $(T_X)_U\hookrightarrow X$, localization for perfect complexes gives

$$
\left(
\operatorname{Perf}(X)/
\operatorname{Perf}_{X\setminus(T_X)_U}(X)
\right)^{\natural}
\simeq\operatorname{Perf}((T_X)_U),
\tag{5.5}
$$

where $(-)^{\natural}$ denotes idempotent completion. This follows from [Nee92, Theorem 2.1]; the supported compact-generation and compactness statements are [Stacks, Tags 0A9A and 0A9B], and perfect extension up to a summand is [Stacks, Tag 09IM].

The ring $R$ is a regular integral finitely generated $\mathbb C$-algebra, its closed points are $\mathbb C$-rational, and $(T_X)_U\to\operatorname{Spec}R$ is smooth and separated. Thus [Mor25, Corollary 2.6] applies and gives

$$
D^b((T_X)_U)/D^b_{R\text{-tors}}((T_X)_U)
\simeq D^b(G_X).
\tag{5.6}
$$

Here the subscript means that the cohomology sheaves are $R$-torsion. The inverse image of this subcategory under restriction from $X$ is exactly the kernel in (5.3). Taking the two localizations successively, and splitting idempotents, therefore yields

$$
\bigl(\operatorname{Perf}(X)/\mathcal T_X\bigr)^{\natural}
\simeq D^b(G_X).
\tag{5.7}
$$

The category on the right is idempotent complete, as $G_X$ is smooth projective. The same construction works for $Y$. Equations (5.4) and (5.7) induce an exact equivalence in (5.1).

Finally, the equivalence is $K$-linear for the common-base identification. Every $a\in K$ can be written as a ratio $s/t$ of global sections of the same sufficiently high power $A^r$, with $t\neq0$. For example, choose a denominator section vanishing along the polar divisor of $a$; normality of $B$ then makes $at$ regular. In the localized category, the endomorphism

$$
t_E^{-1}s_E
$$

is multiplication by $a$. Compatibility of section maps carries it to $\rho(t)_{\Phi(E)}^{-1}\rho(s)_{\Phi(E)}$, which represents the same scalar under (4.2). This compatibility passes to all direct summands in the completed quotients. Hence (5.1) is $K$-linear. $\square$

### 5.2. Motives in dimensions at most two over the function field

**Lemma 5.2.** Let $V$ and $W$ be smooth projective geometrically integral varieties of the same dimension at most two over a characteristic-zero field $K$. If $D^b(V)\simeq D^b(W)$ by an exact $K$-linear equivalence, then

$$
h(V)\simeq h(W)
$$

in the category of rational Chow motives over $K$.

*Proof.* In dimension two, the assertion is [FV21, Theorem 1.1], which is stated over the given base field. In dimension zero, geometric integrality gives $V=W=\operatorname{Spec}K$.

For completeness, suppose that $V=C$ and $W=D$ are curves. Choose rational zero-cycles of degree one on $C$ and $D$ by dividing a closed point by its degree. They define the usual projectors $\pi_{0,C},\pi_{1,C},\pi_{2,C}$ and similarly for $D$, and decompositions

$$
h(C)=\mathbb 1\oplus h^1(C)\oplus\mathbb L,
\qquad
h(D)=\mathbb 1\oplus h^1(D)\oplus\mathbb L.
$$

Let $P_C,Q_C$ be inverse Fourier–Mukai kernels over $K$. Denote the codimension-$i$ components of their Todd-normalized Mukai vectors by

$$
A_i=v_i(P_C),\qquad B_i=v_i(Q_C),
\qquad
v(P_C)=\operatorname{ch}(P_C)\sqrt{\operatorname{td}(C\times_K D)}.
$$

Grothendieck–Riemann–Roch applied to convolution gives the codimension-one identities [Orl05]

$$
\begin{aligned}
B_0\circ A_2+B_1\circ A_1+B_2\circ A_0&=\Delta_C,\\
A_0\circ B_2+A_1\circ B_1+A_2\circ B_0&=\Delta_D.
\end{aligned}
\tag{5.8}
$$

The codimension-zero components are multiples of the fundamental products and are killed by $\pi_1$ on either side. Sandwiching (5.8) by the degree-one projectors therefore gives

$$
\pi_{1,C}B_1A_1\pi_{1,C}=\pi_{1,C},
\qquad
\pi_{1,D}A_1B_1\pi_{1,D}=\pi_{1,D}.
$$

Insert $\pi_{0,D}+\pi_{1,D}+\pi_{2,D}$ between $B_1$ and $A_1$ in the first identity. The $\pi_{0,D}$ contribution vanishes because $\operatorname{Hom}(\mathbb 1,h^1(C))=0$, and the $\pi_{2,D}$ contribution vanishes because $\operatorname{Hom}(h^1(C),\mathbb L)=0$. These vanishings follow directly from the degree-one projectors acting on codimension-zero classes. The symmetric argument applies to the second identity. Consequently,

$$
\pi_{1,D}A_1\pi_{1,C}:h^1(C)\longrightarrow h^1(D),
\qquad
\pi_{1,C}B_1\pi_{1,D}:h^1(D)\longrightarrow h^1(C)
$$

are inverse. Adjoining the identities on $\mathbb 1$ and $\mathbb L$ proves the curve case. All cycles and morphisms were constructed over $K$. $\square$

**Corollary 5.3.** In the situation of Proposition 5.1, there are inverse rational Chow correspondences between $G_X$ and $G_Y$ defined over $K=\mathbb C(B)$.

*Proof.* Their common dimension is $3-\dim B\in\{0,1,2\}$. Apply Lemma 5.2 to Proposition 5.1. $\square$

## 6. Proof of the main theorem

*Proof of Theorem 1.1.* Derived equivalence of smooth projective complex threefolds preserves their rational Hodge structures degree by degree [ACMV19, Theorem 2(a)]. Thus

$$
H^i(X,\mathbb Q)\simeq H^i(Y,\mathbb Q)
\qquad\text{for every }i.
\tag{6.1}
$$

By Proposition 2.4, it suffices to prove that $h(X)$ and $h(Y)$ are isomorphic modulo $\mathcal I$.

Apply Lemma 4.1 to obtain good minimal models $T_X,T_Y$, a common Iitaka base $B$, and omitted subsets $C_X,C_Y$ of dimension at most one. If either omitted subset dominates $B$, then $\dim B=1$. Proposition 4.3 implies that $X$ and $Y$ are birational. Lemma 3.2 gives an isomorphism modulo $\mathcal I$, and Proposition 2.4, using (6.1), gives the required isomorphism of Chow motives.

Suppose now that neither omitted subset dominates $B$. Choose a nonempty regular affine open $U\subset B$ as in (4.12). The smooth proper families $(T_X)_U$ and $(T_Y)_U$ are open subvarieties of $X$ and $Y$. By Proposition 5.1 and Corollary 5.3, their proper generic fibres have isomorphic rational Chow motives over $K=\mathbb C(B)$.

Resolve the graphs of the birational maps $X\dashrightarrow T_X$ and $Y\dashrightarrow T_Y$, choosing resolutions that are isomorphisms over these open families. This gives smooth projective threefolds $Z_X,Z_Y$ and projective morphisms

$$
\begin{aligned}
&Z_X\longrightarrow X,\qquad Z_X\longrightarrow T_X\longrightarrow B,\\
&Z_Y\longrightarrow Y,\qquad Z_Y\longrightarrow T_Y\longrightarrow B.
\end{aligned}
$$

The morphisms to $X$ and $Y$ are birational. The generic fibres of $Z_X\to B$ and $Z_Y\to B$ are exactly $G_X$ and $G_Y$, because the resolutions are isomorphisms over $U$.

Close the $K$-defined inverse correspondences of Corollary 5.3 in $Z_X\times_B Z_Y$ and $Z_Y\times_B Z_X$. Proposition 3.1 shows that their absolute images are inverse modulo $\mathcal I$. Lemma 3.2 then gives the chain of isomorphisms in the quotient category

$$
h(X)\simeq h(Z_X)\simeq h(Z_Y)\simeq h(Y)
\qquad\bmod\mathcal I.
\tag{6.2}
$$

Apply Proposition 2.4 to the original motives $h(X),h(Y)$, with the Hodge isomorphism (6.1). We conclude that

$$
h(X)_{\mathbb Q}\simeq h(Y)_{\mathbb Q}.
$$

This proves the theorem in all three cases $\kappa(X)=1,2,3$. $\square$

**Remark 6.1.** The Hodge comparison is used only for the original derived-equivalent pair $X,Y$. The smooth graph resolutions need not be derived equivalent or have isomorphic Hodge structures. Similarly, ordinary smooth Chow motives are assigned only to smooth projective varieties; the singular minimal models serve to construct the common base and its proper generic fibres.

**Remark 6.2.** The horizontal case in Proposition 4.3 is necessary even for elementary birational modifications. If $S$ is a projective K3 surface, $p\in S$, and $C$ is a curve of genus at least two, then

$$
X=\operatorname{Bl}_{\{p\}\times C}(S\times C)
\simeq(\operatorname{Bl}_pS)\times C
$$

has Kodaira dimension one. Its exceptional divisor is a horizontal fixed canonical divisor. The canonical free locus has generic fibre $(S\setminus\{p\})_{\mathbb C(C)}$, whereas the generic fibre of the good minimal model is the proper surface $S_{\mathbb C(C)}$. Proposition 4.3 handles the omitted points using the original projective kernel, before any comparison of proper generic derived categories is made.

**Remark 6.3.** Positive Kodaira dimension enters through the inequality $\dim B>0$, which reduces the proper generic fibres to dimension at most two. When $\kappa(X)=0$, the Iitaka base is a point, so this reduction does not address the general three-dimensional case. The support calculation of Lemma 2.1 is also specific to dimension three: after resolving the two boundary divisors, it involves divisor classes on a product of surfaces.

## References

**[ACMV19]** Jeffrey D. Achter, Sebastian Casalaina-Martin, and Charles Vial, *Derived equivalent threefolds, algebraic representatives, and the coniveau filtration*, Mathematical Proceedings of the Cambridge Philosophical Society **167** (2019), no. 1, 123–131. [DOI](https://doi.org/10.1017/S0305004118000221); [arXiv:1704.01902](https://arxiv.org/abs/1704.01902). The degreewise rational Hodge comparison used here is Theorem 2(a).

**[CH07]** Alessio Corti and Masaki Hanamura, *Motivic decomposition and intersection Chow groups II*, Pure and Applied Mathematics Quarterly **3** (2007), no. 1, 181–203. [DOI](https://doi.org/10.4310/PAMQ.2007.v3.n1.a6); [author manuscript](https://www.ma.imperial.ac.uk/~acorti/download/md-ich.pdf). The construction of relative correspondences used here is in §3.1.

**[FA92]** János Kollár (ed.), *Flips and abundance for algebraic threefolds: A summer seminar at the University of Utah (Salt Lake City, 1991)*, Astérisque **211**, Société Mathématique de France, 1992. [Numdam](https://numdam.org/item/AST_1992__211__1_0/). See the overview in Chapter 1 and the threefold minimal model and abundance results developed in the volume.

**[Ful98]** William Fulton, *Intersection Theory*, second edition, Ergebnisse der Mathematik und ihrer Grenzgebiete, 3. Folge, vol. 2, Springer-Verlag, Berlin, 1998. [DOI](https://doi.org/10.1007/978-1-4612-1700-8). We use Chow localization, refined Gysin operations and their compatibilities, and Grothendieck–Riemann–Roch.

**[FV21]** Lie Fu and Charles Vial, *A motivic global Torelli theorem for isogenous K3 surfaces*, Advances in Mathematics **383** (2021), article 107674. [arXiv:1907.10868](https://arxiv.org/abs/1907.10868); [author manuscript](https://irma.math.unistra.fr/~lfu/articles/DerivedEqK3.pdf). See Theorem 1.1 for surfaces over the given field, Theorem 1.4 and equation (3) for the Chow–Künneth statements, and Proposition 1.5 for degree-one curve motives.

**[Kaw92]** Yujiro Kawamata, *Abundance theorem for minimal threefolds*, Inventiones Mathematicae **108** (1992), 229–246. [DOI](https://doi.org/10.1007/BF02100604).

**[Kol90]** János Kollár, *Minimal models of algebraic threefolds: Mori's program*, Séminaire Bourbaki, exp. no. 712, Astérisque **177–178** (1990), 303–326. [Numdam](https://www.numdam.org/item/SB_1988-1989__31__303_0/). See Proposition 2.7 for isolatedness of terminal threefold singularities.

**[Mor25]** Hayato Morimura, *Categorical generic fiber*, Journal of Algebra **667** (2025), 75–108. [DOI](https://doi.org/10.1016/j.jalgebra.2024.12.013); [arXiv:2111.00239](https://arxiv.org/abs/2111.00239). We use Corollary 2.6 under the hypotheses stated at the beginning of §2.

**[Nee92]** Amnon Neeman, *The connection between the $K$-theory localization theorem of Thomason, Trobaugh and Yao and the smashing subcategories of Bousfield and Ravenel*, Annales Scientifiques de l'École Normale Supérieure (4) **25** (1992), no. 5, 547–566. [DOI](https://doi.org/10.24033/asens.1659). See Theorem 2.1 for the localization statement on compact objects.

**[Orl97]** Dmitri Orlov, *Equivalences of derived categories and K3 surfaces*, Journal of Mathematical Sciences **84** (1997), no. 5, 1361–1381. [arXiv:alg-geom/9606006](https://arxiv.org/abs/alg-geom/9606006). See Theorem 2.18 for representability of equivalences.

**[Orl05]** Dmitri Orlov, *Derived categories of coherent sheaves and motives*, Russian Mathematical Surveys **60** (2005), no. 6, 1242–1244. [arXiv:math/0512620](https://arxiv.org/abs/math/0512620). We use the Mukai-vector composition identity obtained from Grothendieck–Riemann–Roch.

**[Stacks]** The Stacks Project Authors, *The Stacks Project*. [Tag 0A9A](https://stacks.math.columbia.edu/tag/0A9A), [Tag 0A9B](https://stacks.math.columbia.edu/tag/0A9B), and [Tag 09IM](https://stacks.math.columbia.edu/tag/09IM). Accessed 14 September 2026.

**[Tod06]** Yukinobu Toda, *Fourier–Mukai transforms and canonical divisors*, Compositio Mathematica **142** (2006), no. 4, 962–982. [arXiv:math/0312015](https://arxiv.org/abs/math/0312015). We use Proposition 4.1, Lemmas 4.2–4.3, Corollary 4.4, and Lemma 7.3; the proof of Theorem 7.5, Step 1, contains the related free-locus comparison in Kodaira dimension two.

Contact Information:
Yuan Lu
ETH Zürich
yuan171003@outlook.com
