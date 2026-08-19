# Initialisation
Avant une expérience, on force l'état de spin à $\ket{m_s = 0}$. Pour ce faire, on envoie de la lumière verte à 523nm à l'aide d'un laser. Cela a pour effet d'envoyer tous les états fondamentaux vers les états excités.

Ainsi, les états de spins $\ket{m_s = 0}$ se désexcite en passant par la transition radiative et les états de spins $\ket{m_s = \pm 1}$ passe avec forte probabilité par l'état singlet puis on une chance de retourner vers l'état fondamental de spin $\ket{m_s = 0}$. En illuminant assez longtemps l'échantillon, on termine avec seulement des états fondamentaux de spins $\ket{m_s = 0}$.

Plus d'informations [ici](https://en.wikipedia.org/wiki/Nitrogen-vacancy_center).


# Détection
Lors d'un changement radiatif de l'état excite vers fondamental, un photon à 637nm est émit. à l'aide d'une diode à avalanche (APD), on peut détecter ces photons uniques. Le signal est envoyé à la carte RFSoC4x2 qui compte le nombre de photons perçus.


# ODMR
L'Optically Detected Magnetic Resonance est une expérience qui permet de déterminer la fréquence micro-onde résonnante (précisément, celle qui envoit $\ket{m_s = 0} vers \ket{m_s = -1} et \ket{m_s = -1} vers \ket{m_s = 0}$).

Pour ce faire, on récolte l'intensité lumineuse. Lorsque la fréquence est résonnante, on pompte des $\ket{m_s=0}$ vers des $\ket{m_s=-1}$ et  ainsi la transition de l'état excité à fondamental à plus de chance de passer par l'état singlet qui est non-radiatif. Ainsi, on perçoit moins d'intensité lumineuse.

Cette fréquence est autour de 2.85GHz, mais varie par [effet Zeeman](https://en.wikipedia.org/wiki/Zeeman_effect). Le cristal du diamant permet 4 orientations différentes du placement du centre NV. En observant plusieurs centres à la fois, il peut y avoir jusqu'à quatres fréquences résonnante différentes. L'écart de fréquence avec la fréquence à $\vert \vec{B} \vert = 0$ est proportionel à $\vec{B} \cdot \vec{I}$ avec $\vec{I}$ l'orientation du défaut.


# Rabi
Les oscillations de Rabi permettent de voir l'état de spin évoluer dans le temps. On apperçoit que la fréquence d'oscillation dépend de l'intensité et du temps pour lequel les micro-ondes sont appliquées. On aperçoit aussi la décohérence de l'état causée par du bruit.

Pour ce faire, on envoie des micro-ondes sur un centre NV à une fréquence résonnante. On fait varier la longueur pulse micro-onde et on observe l'intensité lumineuse reçue. Il est aussi possible de varier l'intensité et observer que la fréquence d'oscillation dépend de l'intensité des pulses micro-ondes.
